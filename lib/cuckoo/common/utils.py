# Copyright (C) 2010-2014 Cuckoo Sandbox Developers.
# This file is part of Cuckoo Sandbox - http://www.cuckoosandbox.org
# See the file 'docs/LICENSE' for copying permission.

import os
import time
import shutil
import ntpath
import string
import tempfile
import xmlrpclib
import errno

from datetime import datetime
from threading import Thread
from threading import Event

from lib.cuckoo.common.exceptions import CuckooOperationalError

try:
    import chardet
    HAVE_CHARDET = True
except ImportError:
    HAVE_CHARDET = False

def create_folders(root=".", folders=[]):
    """Create directories.
    @param root: root path.
    @param folders: folders list to be created.
    @raise CuckooOperationalError: if fails to create folder.
    """
    for folder in folders:
        if os.path.isdir(os.path.join(root, folder)):
            continue
        else:
            create_folder(root, folder)

def create_folder(root=".", folder=None):
    """Create directory.
    @param root: root path.
    @param folder: folder name to be created.
    @raise CuckooOperationalError: if fails to create folder.
    """
    if not os.path.exists(os.path.join(root, folder)) and folder:
        folder_path = os.path.join(root, folder)
        if not os.path.isdir(folder_path):
            try:
                os.makedirs(folder_path)
            except OSError:
                raise CuckooOperationalError("Unable to create folder: %s" %
                                             folder_path)


def delete_folder(folder):
    """Delete a folder and all its subdirectories.
    @param folder: path to delete.
    @raise CuckooOperationalError: if fails to delete folder.
    """
    if os.path.exists(folder):
        try:
            shutil.rmtree(folder)
        except OSError:
            raise CuckooOperationalError("Unable to delete folder: "
                                         "{0}".format(folder))

def create_dir_safe(folder):
	"""Creates a directory. 
	Won't cause an exception if directory already exists.
	"""
	try:
		os.mkdir(folder)
	except OSError as e:
		if e.errno != errno.EEXIST:
			raise
def remove_dir_safe(folder):
	"""Removes a directory. 
	Won't cause an exception if directory does not exist.
	"""
	try:
		shutil.rmtree(folder)
	except OSError as e:
		if e.errno != errno.ENOENT:
			raise

def copy_safe(src, dst):
	"""Copies a file. 
	Won't cause an exception if file does not exist.
	"""
	try:
		shutil.copy(src, dst)
	except IOError as e:
		if e.errno != errno.ENOENT:
			raise

# don't allow all characters in "string.printable", as newlines, carriage
# returns, tabs, \x0b, and \x0c may mess up reports
PRINTABLE_CHARACTERS = string.letters + string.digits + string.punctuation + " \t\r\n"


def convert_char(c):
    """Escapes characters.
    @param c: dirty char.
    @return: sanitized char.
    """
    if c in PRINTABLE_CHARACTERS:
        return c
    else:
        return "\\x%02x" % ord(c)


def is_printable(s):
    """ Test if a string is printable."""
    for c in s:
        if not c in PRINTABLE_CHARACTERS:
            return False
    return True

def convert_to_printable(s):
    """Convert char to printable.
    @param s: string.
    @return: sanitized string.
    """
    if is_printable(s):
        return s
    return "".join(convert_char(c) for c in s)

def datetime_to_iso(timestamp):
    """Parse a datatime string and returns a datetime in iso format.
    @param timestamp: timestamp string
    @return: ISO datetime
    """  
    return datetime.strptime(timestamp, "%Y-%m-%d %H:%M:%S").isoformat()

def get_filename_from_path(path):
    """Cross-platform filename extraction from path.
    @param path: file path.
    @return: filename.
    """
    dirpath, filename = ntpath.split(path)
    return filename if filename else ntpath.basename(dirpath)

def store_temp_file(filedata, filename):
    """Store a temporary file.
    @param filedata: content of the original file.
    @param filename: name of the original file.
    @return: path to the temporary file.
    """
    filename = get_filename_from_path(filename)

    # reduce length (100 is arbitrary)
    filename = filename[:100]

    tmppath = tempfile.gettempdir()
    targetpath = os.path.join(tmppath, "cuckoo-tmp")
    if not os.path.exists(targetpath):
        os.mkdir(targetpath)

    tmp_dir = tempfile.mkdtemp(prefix="upload_", dir=targetpath)
    tmp_file_path = os.path.join(tmp_dir, filename)
    with open(tmp_file_path, "wb") as tmp_file:
        # if filedata is file object, do chunked copy
        if hasattr(filedata, "read"):
            chunk = filedata.read(1024)
            while chunk:
                tmp_file.write(chunk)
                chunk = filedata.read(1024)
        else:
            tmp_file.write(filedata)

    return tmp_file_path

# Changed: Added this class
class _ResumableTimer(Thread):
    """Call a function after a specified number of seconds:

            t = Timer(30.0, f, args=[], kwargs={})
            t.start()
            t.cancel()     # stop the timer's action if it's still waiting

    """

    def __init__(self, interval, function, args=[], kwargs={}):
        Thread.__init__(self)
        self.interval = interval
        self.function = function
        self.args = args
        self.kwargs = kwargs
        self.finished = Event()
        self.stopev = Event()
        
    def stop(self):
        self.stopev.set()
    def resume(self):
        self.stopev.clear()
    def cancel(self):
        """Stop the timer if it hasn't finished yet"""
        self.finished.set()
    
    def run(self):
        t = 0
        while True:
            if self.finished.is_set() or t >= self.interval:
                break
            while self.stopev.is_set():
                time.sleep(0.005)
            time.sleep(0.005)
            t += 0.005
        if not self.finished.is_set():
            self.function(*self.args, **self.kwargs)
        self.finished.set()
    
        
def ResumableTimer(*args, **kwargs):
    return _ResumableTimer(*args, **kwargs)

class TimeoutServer(xmlrpclib.ServerProxy):
    """Timeout server for XMLRPC.
    XMLRPC + timeout - still a bit ugly - but at least gets rid of setdefaulttimeout
    inspired by http://stackoverflow.com/questions/372365/set-timeout-for-xmlrpclib-serverproxy
    (although their stuff was messy, this is cleaner)
    @see: http://stackoverflow.com/questions/372365/set-timeout-for-xmlrpclib-serverproxy
    """
    def __init__(self, *args, **kwargs):
        timeout = kwargs.pop("timeout", None)
        kwargs["transport"] = TimeoutTransport(timeout=timeout)
        xmlrpclib.ServerProxy.__init__(self, *args, **kwargs)

    def _set_timeout(self, timeout):
        t = self._ServerProxy__transport
        t.timeout = timeout
        # If we still have a socket we need to update that as well.
        if hasattr(t, "_connection") and t._connection[1] and t._connection[1].sock:
            t._connection[1].sock.settimeout(timeout)

class TimeoutTransport(xmlrpclib.Transport):
    def __init__(self, *args, **kwargs):
        self.timeout = kwargs.pop("timeout", None)
        xmlrpclib.Transport.__init__(self, *args, **kwargs)

    def make_connection(self, *args, **kwargs):
        conn = xmlrpclib.Transport.make_connection(self, *args, **kwargs)
        if not self.timeout is None:
            conn.timeout = self.timeout
        return conn

class Singleton(type):
    """Singleton.
    @see: http://stackoverflow.com/questions/6760685/creating-a-singleton-in-python
    """
    _instances = {}
    def __call__(cls, *args, **kwargs):
        if cls not in cls._instances:
            cls._instances[cls] = super(Singleton, cls).__call__(*args, **kwargs)
        return cls._instances[cls]

def logtime(dt):
    """Formats time like a logger does, for the csv output
       (e.g. "2013-01-25 13:21:44,590")
    @param dt: datetime object
    @return: time string
    """
    t = time.strftime("%Y-%m-%d %H:%M:%S", dt.timetuple())
    s = "%s,%03d" % (t, dt.microsecond/1000)
    return s

def time_from_cuckoomon(s):
    """Parse time string received from cuckoomon via netlog
    @param s: time string
    @return: datetime object
    """
    return datetime.strptime(s, "%Y-%m-%d %H:%M:%S,%f")

def to_unicode(s):
    """Attempt to fix non uft-8 string into utf-8. It tries to guess input encoding,
    if fail retry with a replace strategy (so undetectable chars will be escaped).
    @see: fuller list of encodings at http://docs.python.org/library/codecs.html#standard-encodings
    """

    def brute_enc(s2):
        """Trying to decode via simple brute forcing."""
        encodings = ("ascii", "utf8", "latin1")
        for enc in encodings:
            try:
                return unicode(s2, enc)
            except UnicodeDecodeError:
                pass
        return None

    def chardet_enc(s2):
        """Guess encoding via chardet."""
        enc = chardet.detect(s2)["encoding"]

        try:
            return unicode(s2, enc)
        except UnicodeDecodeError:
            pass
        return None

    # If already in unicode, skip.
    if isinstance(s, unicode):
        return s

    # First try to decode against a little set of common encodings.
    result = brute_enc(s)

    # Try via chardet.
    if (not result) and HAVE_CHARDET:
        result = chardet_enc(s)

    # If not possible to convert the input string, try again with
    # a replace strategy.
    if not result:
        result = unicode(s, errors="replace")

    return result

def cleanup_value(v):
    """Cleanup utility function, strips some unwanted parts from values."""
    v = str(v)
    if v.startswith("\\??\\"):
        v = v[4:]
    return v

def sanitize_filename(x):
    """Kind of awful but necessary sanitizing of filenames to 
    get rid of unicode problems."""
    out = ""
    for c in x:
        if c in string.letters + string.digits + " _-.":
            out += c
        else:
            out += "_"

    return out
