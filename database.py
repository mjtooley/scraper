from exceptions import *
import requester
import url_mutator as um
import regex
import re
from pymongo import MongoClient
from pymongo.errors import ServerSelectionTimeoutError
import socket
from bson.son import SON

regex = regex.get()

#The database of host sites to parse
class HostList(object):
    def __init__(self, name=None):
        try:
            self._client = MongoClient(host='172.25.12.109', port=27017)  # creates a client to run the database at on the specified server
        except ServerSelectionTimeoutError:
            raise ServerNotRunningError('MongoDB is not currently running on the host server. Try connecting to the host server and '
                                        'enter at the command line "sudo service mongod restart"')
        if name:
            self._db = self._client[name] # for testing purposes
        else:
            self._db = self._client['hosts']  # the database running on the client

    # adds a host to the database if it isn't already present
    def add_to_hosts(self, host, running=False):
        if not requester.validate_url(host):
            raise InvalidUrlError('Cannot add a url to streams with an invalid host: %s' % host)
        entry = self.entry_from_host(host)
        if entry:
            raise EntryInDatabaseError('The following host is already in the database: %s' % host)
        data = {'host': um.prepare_netloc(host), 'running': running} # data for the entry
        self._db.hosts.insert(data)

    #retrieves an entry in the database corresponding to the inputted host, returns None if no entry is found
    def entry_from_host(self, host):
        if not requester.validate_url(host):
            raise InvalidUrlError('Cannot retrieve entry with an invalid host: %s' % host)
        cursors = self._db.hosts.find({'host': um.prepare_netloc(host)})
        if cursors.count() > 1:  # count counts the number of cursor, if there are multiple cursors an error is raised
            raise MultipleEntriesInDatabaseError('There are multiple entries in the hosts database with the same host: %s' % host)
        if cursors.count() == 1:
            return cursors[0]  # returns the cursor corresponding to the url
        return None  # returns None if no cursor is found

    # retrieves an entry in the database that is not currently running
    def find_not_running_entry(self):
        return self._db.hosts.find_one({'running': False})

    # updates the running status of an inputted host
    def update_running(self, host, running):
        if not requester.validate_url(host):
            raise InvalidUrlError('Cannot update running entry with an invalid host: %s' % host)
        if self.entry_from_host(host) is None:
            raise EntryNotInDatabaseError('The host %s is not in the hosts database' % host)
        self._db.hosts.update({'host': host}, {'$set': {'running': running}})

    # resets the running status of all hosts to False
    def reset_running(self):
        cursors = self._db.hosts.find({'running': True})
        for cursor in cursors:
            host = cursor['host']
            self.update_running(host, False)

    # returns database for testing purposes
    def database(self):
        return self._db

    # deletes database for testing purposes
    def delete(self):
        self._client.drop_database(self._db.name)


"""
Class: Streamer
Purpose: Keeps track of the network locations of streams (ts, mp4, mkv, etc.) that have been found, the IP address of the network location,
and the site that led to the stream. Provides methods for analyzing common patterns within the streams found, such as shared IP addresses,
network locations with the most links pointing to them, etc.
"""


class Streamer(object):
    """
        Method: __init__
        Purpose: instantiates an instance of the visitor class
        Inputs:
            name: the name of the database which the visitor instance will write
        Returns:
            n/a
        Raises:
            InvalidInputError: if the inputted name is not a string
            ServerNotRunningError: if the server the database is stored on is not currently running
        """

    def __init__(self, time):
        if time is None or not isinstance(time, str):
            raise InvalidInputError('Cannot write to a database with an invalid collection name: %s' % time)
        self._name = 'streams'
        try:
            self._client = MongoClient(host='172.25.12.109', port=27017)  # creates a client to run the database at on the specified server
        except ServerSelectionTimeoutError:
            raise ServerNotRunningError('MongoDB is not currently running on the host server. Try connecting to the host server and '
                                        'enter at the command line "sudo service mongod restart"')
        self._db = self._client['streams']  # the database running on the client
        self._collection = self._db[time]
        self.broken_stream_links = set()  # keeps track of network location known to lead to invalid streams,
        # prevents redundant searching in the database
        self.working_stream_links = set() # keeps track of network location known to lead to working streams,
        # prevents redundant searching the database
        self.ip_addresses = {} # keeps track of IP address of network locations to prevent redundant requests
        self.connection_attempts = {} # keeps tracks of the connection attempts a given network location has made to check for
        # stream validity
        self.fibs = fib_to(10) # a list of fibonacci numbers, increase the input to this function and the algorithm will make
        # more attempts to check the validity of a given stream, decrease the input and the algorithm will make less attempts
        # to check the validity of a given stream. Note that it will check the inputted number minus one streams, so if the input is
        # fib_to(20), 19 streams of a given netloc would be checked before declaring it invalid

    def add_to_streams(self, url, host):
        if not requester.validate_url(url):
            raise InvalidUrlError('Cannot add an invalid url to streams: %s' % url)
        if not requester.validate_url(host):
            raise InvalidUrlError('Cannot add a url to streams with an invalid host: %s' % host)
        netloc = um.prepare_netloc(url)
        if netloc not in self.broken_stream_links and netloc not in self.working_stream_links: # Note that network locations are only
            # added to broken_stream_links or working_stream_links if their working link status is known. Also note that visitor classes
            # are unique to crawler classes, therefore each visitor class will only deal with one host. Therefore if a stream is
            # added to the database from a given visitor class with a known working link status, then it is no longer necessary
            # to evaluate that stream, doing so would lead to redundant requests to the database
            if netloc not in self.ip_addresses: #if there isn't an IP address assigned to the network location
                try:
                    ip_addresses = socket.gethostbyname_ex(um.remove_schema(netloc))[2] #fetches all IP addresses from the network location
                    self.ip_addresses[netloc] = ip_addresses
                except socket.gaierror: #if this error is raised then the network location is down
                    ip_addresses = None
            else:
                ip_addresses = self.ip_addresses[netloc]
            if ip_addresses:
                stream_statuses = {}
                for ip_address in ip_addresses:
                    if (ip_address, netloc) not in self.connection_attempts:
                        self.connection_attempts[(ip_address, netloc)] = 1
                    if self.connection_attempts[(ip_address, netloc)] in self.fibs:
                        doc = self.document_from_ip_address(ip_address)
                        try:
                            entry_from_netloc = self.entry_from_netloc(doc, netloc)
                        except InvalidInputError:
                            entry_from_netloc = None
                        if entry_from_netloc and entry_from_netloc['working_link']:
                            working_link = True
                        elif url not in stream_statuses:
                            try:
                                stream_status = requester.evaluate_stream(url)
                            except StreamTimedOutError:
                                stream_statuses[url] = working_link = False
                            else:
                                if stream_status:
                                    stream_statuses[url] = working_link = True
                                elif self.connection_attempts[(ip_address, netloc)] == self.fibs[-1]:
                                    stream_statuses[url] = working_link = False
                                else:
                                    stream_statuses[url] = working_link = None
                        else:
                            working_link = stream_statuses[url]
                        self.add_to_database_by_ip_address(ip_address, netloc, host, working_link)
                        if working_link:
                            self.working_stream_links.add(netloc)
                        elif working_link is False:
                            self.broken_stream_links.add(netloc)
                    self.connection_attempts[(ip_address, netloc)] += 1
            else:
                #print('%s is an invalid stream as the network location is deprecated' % url)
                self.broken_stream_links.add(netloc)
                self.add_to_database_by_ip_address(None, netloc, host, False)
                
    def add_to_database_by_ip_address(self, ip_address, netloc, host, working_link):
        if ip_address is not None and not re.search(regex['ip'], ip_address):
            raise InvalidInputError('Cannot add to database with invalid IP address: %s' % ip_address)
        if not requester.validate_url(netloc):
            raise InvalidUrlError('Cannot add to database with an invalid url: %s' % netloc)
        if not requester.validate_url(host):
            raise InvalidUrlError('Cannot add to database with an invalid host: %s' % host)
        doc = self.document_from_ip_address(ip_address)
        if not doc:
            data = {
                'ip_address': ip_address,
                'network_locations': [SON([('network_location', netloc), ('linked_by', [host]), ('working_link', working_link)])]
            }
            self._collection.insert(data)
        else:
            entry_from_netloc = self.entry_from_netloc(doc, netloc)
            if not entry_from_netloc:
                subdata = SON([('network_location', netloc), ('linked_by', [host]), ('working_link', working_link)])
                self._collection.update({'ip_address': ip_address}, {'$push': {'network_locations': subdata}})
            else:
                if host not in entry_from_netloc['linked_by']:
                    #print('%s is not in the linked_by of %s at %s' % (host, netloc, ip_address))
                    self._collection.update({'ip_address': ip_address, 'network_locations.network_location': netloc},
                                              {'$push': {'network_locations.$.linked_by': host}})
                current_working_link = entry_from_netloc['working_link']
                if working_link is not None:
                    if working_link and not current_working_link or working_link is False and current_working_link is None:
                        #print('Updating the working link status of %s at %s to %s as it was previously %s' %
                        #      (netloc, ip_address, working_link, current_working_link))
                        self._collection.update({'ip_address': ip_address, 'network_locations.network_location': netloc},
                                                  {'$set': {'network_locations.$.working_link': working_link}})

    def entry_from_netloc(self, doc, netloc):
        if doc is None:
            raise InvalidInputError('Cannot retrieve entry from netloc with null document')
        if not requester.validate_url(netloc):
            raise InvalidUrlError('Cannot retrieve entry from netloc with invalid netloc: %s' % netloc)
        efn = None
        for entry in doc['network_locations']:
            if entry['network_location'] == netloc:
                efn = entry
        return efn

    def document_from_ip_address(self, ip_address):
        if ip_address is not None and not re.search(regex['ip'], ip_address):
            raise InvalidInputError('Cannot add to database with invalid IP address: %s' % ip_address)
        cursors = self._collection.find({'ip_address': ip_address})  # retrieves all cursors that contain the inputted IP
        if cursors.count() > 1:  # count counts the number of cursor, if there are multiple cursors an error is raised
            raise MultipleEntriesInDatabaseError('There are multiple entries in the %s with the same IP address: %s' %
                                                 (self._name, ip_address))
        if cursors.count() == 1:
            return cursors[0]  # returns the cursor corresponding to the url
        return None  # returns None if no cursor is found

    def database(self):
        return self._db

    def delete(self):
        self._client.drop_database(self._db.name)


"""
Method: fib_to
Purpose: returns an array of fibonacci numbers up to n
Example:
    fib_to(5) -> [0, 1, 1, 2, 3]
Input:
    n, number to fib to
Returns: array of fibonacci numbers
Raises:
    InvalidInputError, if n is not an integer
"""

def fib_to(n):
    if not isinstance(n, int):
        raise InvalidInputError('Cannot fib to with a non-integer input: %s' % str(n))
    fibs = [0, 1]
    for i in range(2, n + 1):
        fibs.append(fibs[-1] + fibs[-2])
    return fibs
