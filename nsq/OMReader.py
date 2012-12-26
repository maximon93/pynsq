"""

One More Reader.

high-level NSQ reader class built on top of a select.poll() supporting async (callback) mode of operation.

Supports multiple nsqd connections using plain list of hosts, multiple A-records or (near future) SRV-requests.

Differences from NSQReader:
1. Message processing assume to be independent from message to message. One failed message do not make slower overall message processing (not using BackoffTimer at all).
2. We don't use lookupd server query since all required nsqd may be defined through DNS.
3. Max message attempts and requeue interval is controlled by user-side. The only requeue delay is for failed callbacks
4. OMReader do not use tornado. It uses select.poll() object for handling multiple asyn connections
5. As of 5, all OMReader instances are isolated. You can run and stop them as you wish.

"""

import time
import select
import socket
import random
import nsq
import async
import logging
import sys
import os
import struct

import nsq

class OMReader(object):
    def __init__(self, message_callback, topic, channel=None, nsqd_addresses=None, max_in_flight=1, requeue_delay=90):
        
        self.message_callback = message_callback
        self.topic = topic
        self.channel = channel or topic
        self.max_in_flight = max_in_flight
        self.requeue_delay = requeue_delay
        
        self.nsqd_addresses = nsqd_addresses
        self.nsqd_tcp_addresses = nsqd_addresses
        self.poll = select.poll()
        self.shutdown = False

        self.hostname = socket.gethostname()
        self.short_hostname = self.hostname.split('.')[0]
        
    def resolve_nsqd_addresses(self):
        pass


    def connect(self):
        self.connections = {}
        self.connections_last_check = time.time()
        
        for address in self.nsqd_tcp_addresses:
            self.connect_one(address)

    def connect_one(self, address):
        logging.debug("Connecting to %s", address)
        
        self.connections[address] = {
            'socket': None,
            'state': 'connecting',
            'time': time.time(),
            #'out': '', # output buffer
            'in': '', # incoming buffer
            'closed': True,
        }
        
        try:
            sock = socket.create_connection(address, 5)
        except socket.error:
            logging.error("Error connecting: %s" % sys.exc_info()[1])
            return None

        self.connections[address]['socket'] = sock
        self.connections[address]['state'] = 'connected'
        self.connections[address]['closed'] = False
        
        sock.setblocking(0)
        
        sock.send(nsq.MAGIC_V2)
        sock.send(nsq.subscribe(self.topic, self.channel, self.short_hostname, self.hostname))
        sock.send(nsq.ready(self.max_in_flight))

        # maybe, poll POLLOUT as well, but it is so boring
        self.poll.register(sock, select.POLLIN + select.POLLPRI + select.POLLERR + select.POLLHUP)
        

    def connections_check(self):
        to_connect = []
        
        for address, item in self.connections.items():
            if (item['state'] == 'disconnected' or item['state'] == 'connecting') and (time.time() - item['time']) > 5:
                to_connect.append(address)
        
        
        
        for address in to_connect:
            if not self.connections[address]['closed']:
                self.connection_close(address)
            del(self.connections[address])

        for address in to_connect:
            self.connect_one(address)

        self.connections_last_check = time.time()

    def connection_search(self, fd):
        item = None
        for address, item in self.connections.items():
            if item['socket'] and item['socket'].fileno() == fd:
                break
        
        return address, item

            

    def connection_close(self, address):
        logging.debug("unregistering and closing connection to %s", address)
        
        item = self.connections[address]
        self.poll.unregister(item['socket'])
        
        item['socket'].close()
        item['state'] = 'disconnected'
        item['closed'] = True
        item['time'] = time.time()
        

    def process_data(self, address, data):
        """Process incoming data. All return data will remain as rest of buffer"""
        res = None
        if len(data) >= 4:
            packet_length = struct.unpack('>l', data[:4])[0]
            if len(data) >= 4 + packet_length:
                # yeah, complete response received

                frame_id, frame_data = nsq.unpack_response(data[4 : packet_length + 4])
                if frame_id == nsq.FRAME_TYPE_MESSAGE:
                    self.connections[address]['socket'].send(nsq.ready(self.max_in_flight))
                    message = nsq.decode_message(frame_data)
                    
                    try:
                        res, delay = self.message_callback(message)
                    except:
                        logging.error("Error calling message callback. Requeuing message with default queue delay. Exception: %s", sys.exc_info()[1])
                        res, delay = False, self.requeue_delay

                    if res:
                        logging.info("Message %s processed successfully", message.id)
                        self.connections[address]['socket'].sendall(nsq.finish(message.id))
                    else:
                        logging.info("Requeueing message %s with delay %d", message.id, delay)
                        self.connections[address]['socket'].sendall(nsq.requeue(message.id, str(int(delay * 1000))))
                    
                elif frame_id == nsq.FRAME_TYPE_ERROR:
                    logging.error("Error received. Frame data: %s", repr(frame_data))
                    
                elif frame_id == nsq.FRAME_TYPE_RESPONSE:
                    if frame_data == '_heartbeat_':
                        logging.debug("hearbeat received. Answering with nop")
                        self.connections[address]['socket'].send(nsq.nop())
                    else:
                        logging.debug("Unknown response received. Frame data: %s", repr(frame_data))
                else:
                    logging.error("Unknown frame_id received: %s. Frame data: %s", frame_id, repr(frame_data))
                
                
                res = data[4 + packet_length : ]
                
        return res

    def stop(self):
        self.shutdown = True
        for address, item in self.connections.items():
            logging.debug("Sending CLS to %s", address)
            item['socket'].sendall(nsq.cls())

        
    def run(self):
        
        self.connect()

        logging.info("Starting OMReader for topic '%s'..." % self.topic)
        
        while not self.shutdown:
            time.sleep(0.00001)
            data = self.poll.poll(1)
            
            closed_addresses = []
            
            for fd, event in data:
                
                address, item = self.connection_search(fd)
                if not item:
                    continue
                
                
                if event & select.POLLIN or event & select.POLLPRI:
                    logging.debug("%s: There are data to read", address)
                    data = item['socket'].recv(8192)
                    if data:
                        item['in'] += data
                        
                        while len(item['in']) > 4:
                            old_len = len(item['in'])
                            data_new = self.process_data(address, item['in'])
                            
                            if not data_new is None:
                                item['in'] = data_new
                            
                            if data_new is None or old_len == len(data_new):
                                # data not changed, breaking loop
                                break
                            
                        
                    else:
                        logging.warning("%s Socket closed", address)
                        if not address in closed_addresses:
                            closed_addresses.append(address)

                    
                ## elif event & select.POLLOUT:
                ##     if item['out']:
                ##         logging.(print "%s: Can write data there" % (address,)
                ##         item['socket'].send(item['out'])
                ##         item['out'] = ''
                        
                elif event & select.POLLHUP or event & select.POLLERR:
                    logging.warning("%s: Socket failed. Need reconnecting", address)
                    
                    if not address in closed_addresses:
                        closed_addresses.append(address)

            for address in closed_addresses:
                self.connection_close(address)
            
            
            if time.time() - self.connections_last_check > 5:
                self.connections_check()
            
            

    
        

if __name__ == '__main__':

    logging.basicConfig(filename="omreader.log", level=logging.DEBUG)

    total = 0
    def test_callback(message):
        global total, reader
        logging.info("Received message id: %s, timestamp: %s, attempts: %d, body: %s", message.id, message.timestamp, message.attempts, message.body)
        
        total += 1
        
        logging.info("Total: %d", total)

        if total >= 1000:
            reader.stop()
        
        return random.choice([True, False, True]), 5
    

    addresses = [('localhost', 4150)]
    reader = OMReader(test_callback, sys.argv[1], sys.argv[1], addresses)
    
    reader.run()

    
            