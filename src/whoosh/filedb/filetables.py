# Copyright 2009 Matt Chaput. All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are met:
#
#    1. Redistributions of source code must retain the above copyright notice,
#       this list of conditions and the following disclaimer.
#
#    2. Redistributions in binary form must reproduce the above copyright
#       notice, this list of conditions and the following disclaimer in the
#       documentation and/or other materials provided with the distribution.
#
# THIS SOFTWARE IS PROVIDED BY MATT CHAPUT ``AS IS'' AND ANY EXPRESS OR
# IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED WARRANTIES OF
# MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE DISCLAIMED. IN NO
# EVENT SHALL MATT CHAPUT OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT,
# INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT
# LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA,
# OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF
# LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING
# NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE,
# EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
#
# The views and conclusions contained in the software and documentation are
# those of the authors and should not be interpreted as representing official
# policies, either expressed or implied, of Matt Chaput.

"""This module defines writer and reader classes for a fast, immutable
on-disk key-value database format. The current format is based heavily on
D. J. Bernstein's CDB format (http://cr.yp.to/cdb.html).
"""

from array import array
from collections import defaultdict
from cPickle import loads, dumps
from struct import Struct

from whoosh.matching import ListMatcher
from whoosh.system import (_INT_SIZE, _LONG_SIZE, _FLOAT_SIZE, pack_ushort,
                           pack_uint, pack_long, unpack_ushort, unpack_uint,
                           unpack_long)
from whoosh.util import byte_to_length, length_to_byte, utf8encode, utf8decode


_4GB = 4 * 1024 * 1024 * 1024

def cdb_hash(key):
    h = 5381L
    for c in key:
        h = (h + (h << 5)) & 0xffffffffL ^ ord(c)
    return h

_header_entry_struct = Struct("!qI")  # Position, number of slots
header_entry_size = _header_entry_struct.size
pack_header_entry = _header_entry_struct.pack
unpack_header_entry = _header_entry_struct.unpack

_lengths_struct = Struct("!II")  # Length of key, length of data
lengths_size = _lengths_struct.size
pack_lengths = _lengths_struct.pack
unpack_lengths = _lengths_struct.unpack


# Table classes

class HashWriter(object):
    def __init__(self, dbfile, old_format=False):
        self.dbfile = dbfile
        self.old_format = old_format
        
        if not old_format:
            self.header_size = 16 + 256 * header_entry_size
            _pointer_struct = Struct("!Iq")  # Hash value, position
            self.hash_func = cdb_hash
        else:
            self.header_size = 256 * header_entry_size
            _pointer_struct = Struct("!qq")  # Hash value, position
            self.hash_func = hash
        
        self.pointer_size = _pointer_struct.size
        self.pack_pointer = _pointer_struct.pack
        
        # Seek past the first "header_size" bytes of the file... we'll come
        # back here to write the header later
        dbfile.seek(self.header_size)
        # Store the directory of hashed values
        self.hashes = defaultdict(list)

    def add_all(self, items):
        dbfile = self.dbfile
        hash_func = self.hash_func
        hashes = self.hashes
        pos = dbfile.tell()
        write = dbfile.write

        for key, value in items:
            write(pack_lengths(len(key), len(value)))
            write(key)
            write(value)

            h = hash_func(key)
            hashes[h & 255].append((h, pos))
            pos += lengths_size + len(key) + len(value)

    def add(self, key, value):
        self.add_all(((key, value),))

    def _write_hashes(self):
        dbfile = self.dbfile
        hashes = self.hashes
        directory = self.directory = []

        pos = dbfile.tell()
        for i in xrange(0, 256):
            entries = hashes[i]
            numslots = 2 * len(entries)
            directory.append((pos, numslots))

            null = (0, 0)
            hashtable = [null] * numslots
            for hashval, position in entries:
                n = (hashval >> 8) % numslots
                while hashtable[n] != null:
                    n = (n + 1) % numslots
                hashtable[n] = (hashval, position)

            write = dbfile.write
            for hashval, position in hashtable:
                write(self.pack_pointer(hashval, position))
                pos += self.pointer_size

        dbfile.flush()
        self._end_of_hashes = dbfile.tell()

    def _write_directory(self):
        dbfile = self.dbfile
        directory = self.directory

        dbfile.seek(0)
        if not self.old_format:
            dbfile.write("HASH")
            dbfile.write_int(0)  # Unused
            dbfile.write_long(self._end_of_hashes)
        
        for position, numslots in directory:
            dbfile.write(pack_header_entry(position, numslots))
        
        dbfile.flush()
        assert dbfile.tell() == self.header_size

    def close(self):
        self._write_hashes()
        self._write_directory()
        self.dbfile.close()


class HashReader(object):
    def __init__(self, dbfile):
        self.dbfile = dbfile
        self.map = dbfile.map
        
        dbfile.seek(0)
        magic = dbfile.read(4)
        if magic == "HASH":
            self.old_format = False
            self.header_size = 16 + 256 * header_entry_size
            _pointer_struct = Struct("!Iq")  # Hash value, position
            dbfile.read_int()  # Unused
            self._end_of_hashes = dbfile.read_long()
            assert self._end_of_hashes >= self.header_size, "%s < %s" % (self._end_of_hashes, self.header_size)
            self.hash_func = cdb_hash
        else:
            self.old_format = True
            self.header_size = 256 * header_entry_size
            _pointer_struct = Struct("!qq")  # Hash value, position
            self.hash_func = hash
        
        self.buckets = []
        for _ in xrange(256):
            he = unpack_header_entry(dbfile.read(header_entry_size))
            self.buckets.append(he)
        self._start_of_hashes = self.buckets[0][0]
        
        self.pointer_size = _pointer_struct.size
        self.unpack_pointer = _pointer_struct.unpack

        self.is_closed = False

    def close(self):
        if self.is_closed:
            raise Exception("Tried to close %r twice" % self)
        del self.map
        self.dbfile.close()
        self.is_closed = True

    def read(self, position, length):
        return self.map[position:position + length]

    def _ranges(self, pos=None):
        if pos is None:
            pos = self.header_size
        eod = self._start_of_hashes
        read = self.read
        while pos < eod:
            keylen, datalen = unpack_lengths(read(pos, lengths_size))
            keypos = pos + lengths_size
            datapos = pos + lengths_size + keylen
            pos = datapos + datalen
            yield (keypos, keylen, datapos, datalen)

    def __iter__(self):
        return self.items()

    def items(self):
        read = self.read
        for keypos, keylen, datapos, datalen in self._ranges():
            yield (read(keypos, keylen), read(datapos, datalen))

    def keys(self):
        read = self.read
        for keypos, keylen, _, _ in self._ranges():
            yield read(keypos, keylen)

    def values(self):
        read = self.read
        for _, _, datapos, datalen in self._ranges():
            yield read(datapos, datalen)

    def __getitem__(self, key):
        for data in self.all(key):
            return data
        raise KeyError(key)

    def get(self, key, default=None):
        for data in self.all(key):
            return data
        return default

    def all(self, key):
        read = self.read
        for datapos, datalen in self.ranges_for_key(key):
            yield read(datapos, datalen)

    def __contains__(self, key):
        for _ in self.ranges_for_key(key):
            return True
        return False

    def _hashtable_info(self, keyhash):
        # Return (directory_position, number_of_hash_entries)
        return self.buckets[keyhash & 255]

    def _key_position(self, key):
        keyhash = self.hash_func(key)
        hpos, hslots = self._hashtable_info(keyhash)
        if not hslots:
            raise KeyError(key)
        slotpos = hpos + (((keyhash >> 8) % hslots) * header_entry_size)
        
        return self.dbfile.get_long(slotpos + _INT_SIZE)

    def _key_at(self, pos):
        keylen = self.dbfile.get_uint(pos)
        return self.read(pos + lengths_size, keylen)

    def ranges_for_key(self, key):
        read = self.read
        pointer_size = self.pointer_size
        keyhash = self.hash_func(key)
        hpos, hslots = self._hashtable_info(keyhash)
        if not hslots:
            return

        slotpos = hpos + (((keyhash >> 8) % hslots) * pointer_size)
        for _ in xrange(hslots):
            slothash, pos = self.unpack_pointer(read(slotpos, pointer_size))
            if not pos:
                return

            slotpos += pointer_size
            # If we reach the end of the hashtable, wrap around
            if slotpos == hpos + (hslots * pointer_size):
                slotpos = hpos

            if slothash == keyhash:
                keylen, datalen = unpack_lengths(read(pos, lengths_size))
                if keylen == len(key):
                    if key == read(pos + lengths_size, keylen):
                        yield (pos + lengths_size + keylen, datalen)
    
    def range_for_key(self, key):
        for item in self.ranges_for_key(key):
            return item
        raise KeyError(key)
    
    def end_of_hashes(self):
        if self.old_format:
            lastpos, lastnum = self.buckets[255]
            return lastpos + lastnum * self.pointer_size
        else:
            return self._end_of_hashes


class OrderedHashWriter(HashWriter):
    def __init__(self, dbfile):
        HashWriter.__init__(self, dbfile)
        self.index = []
        self.lastkey = None

    def add_all(self, items):
        dbfile = self.dbfile
        hashes = self.hashes
        hash_func = self.hash_func
        pos = dbfile.tell()
        write = dbfile.write

        index = self.index
        lk = self.lastkey

        for key, value in items:
            if key <= lk:
                raise ValueError("Keys must increase: %r .. %r" % (lk, key))
            lk = key

            index.append(pos)
            write(pack_lengths(len(key), len(value)))
            write(key)
            write(value)

            h = hash_func(key)
            hashes[h & 255].append((h, pos))
            
            pos += lengths_size + len(key) + len(value)
        
        self.lastkey = lk

    def close(self):
        self._write_hashes()
        dbfile = self.dbfile
        
        dbfile.write_uint(len(self.index))
        for n in self.index:
            dbfile.write_long(n)
        
        self._write_directory()
        self.dbfile.close()


class OrderedHashReader(HashReader):
    def __init__(self, dbfile):
        HashReader.__init__(self, dbfile)
        dbfile.seek(self.end_of_hashes())
        self.length = dbfile.read_uint()
        self.indexbase = dbfile.tell()
    
    def _closest_key(self, key):
        dbfile = self.dbfile
        key_at = self._key_at
        indexbase = self.indexbase
        lo = 0
        hi = self.length
        while lo < hi:
            mid = (lo + hi) // 2
            midkey = key_at(dbfile.get_long(indexbase + mid * _LONG_SIZE))
            if midkey < key:
                lo = mid + 1
            else:
                hi = mid
        #i = max(0, mid - 1)
        if lo == self.length:
            return None
        return dbfile.get_long(indexbase + lo * _LONG_SIZE)
    
    def closest_key(self, key):
        pos = self._closest_key(key)
        if pos is None:
            return None
        return self._key_at(pos)

    def _ranges_from(self, key):
        #read = self.read
        pos = self._closest_key(key)
        if pos is None:
            return

        for x in self._ranges(pos=pos):
            yield x

    def items_from(self, key):
        read = self.read
        for keypos, keylen, datapos, datalen in self._ranges_from(key):
            yield (read(keypos, keylen), read(datapos, datalen))

    def keys_from(self, key):
        read = self.read
        for keypos, keylen, _, _ in self._ranges_from(key):
            yield read(keypos, keylen)

    def values_from(self, key):
        read = self.read
        for _, _, datapos, datalen in self._ranges_from(key):
            yield read(datapos, datalen)


class CodedHashWriter(HashWriter):
    # Abstract base class, subclass must implement keycoder and valuecoder
    
    def __init__(self, dbfile):
        sup = super(CodedHashWriter, self)
        sup.__init__(dbfile)

        self._add = sup.add
        
    def add(self, key, data):
        self._add(self.keycoder(key), self.valuecoder(data))
        

class CodedHashReader(HashReader):
    # Abstract base class, subclass must implement keycoder, keydecoder and
    # valuecoder
    
    def __init__(self, dbfile):
        sup = super(CodedHashReader, self)
        sup.__init__(dbfile)

        self._items = sup.items
        self._keys = sup.keys
        self._get = sup.get
        self._getitem = sup.__getitem__
        self._contains = sup.__contains__
        
    def __getitem__(self, key):
        k = self.keycoder(key)
        return self.valuedecoder(self._getitem(k))

    def __contains__(self, key):
        return self._contains(self.keycoder(key))

    def get(self, key, default=None):
        k = self.keycoder(key)
        return self.valuedecoder(self._get(k, default))

    def items(self):
        kd = self.keydecoder
        vd = self.valuedecoder
        for key, value in self._items():
            yield (kd(key), vd(value))

    def keys(self):
        kd = self.keydecoder
        for k in self._keys():
            yield kd(k)


class CodedOrderedWriter(OrderedHashWriter):
    # Abstract base class, subclasses must implement keycoder and valuecoder
    
    def __init__(self, dbfile):
        sup = super(CodedOrderedWriter, self)
        sup.__init__(dbfile)
        self._add = sup.add

    def add(self, key, data):
        self._add(self.keycoder(key), self.valuecoder(data))


class CodedOrderedReader(OrderedHashReader):
    # Abstract base class, subclasses must implement keycoder, keydecoder,
    # and valuedecoder
    
    def __init__(self, dbfile):
        sup = super(CodedOrderedReader, self)
        sup.__init__(dbfile)

        self._items = sup.items
        self._items_from = sup.items_from
        self._keys = sup.keys
        self._keys_from = sup.keys_from
        self._get = sup.get
        self._getitem = sup.__getitem__
        self._contains = sup.__contains__
        self._range_for_key = sup.range_for_key

    def __getitem__(self, key):
        k = self.keycoder(key)
        return self.valuedecoder(self._getitem(k))

    def __contains__(self, key):
        try:
            codedkey = self.keycoder(key)
        except KeyError:
            return False
        return self._contains(codedkey)

    def get(self, key, default=None):
        k = self.keycoder(key)
        return self.valuedecoder(self._get(k, default))

    def items(self):
        kd = self.keydecoder
        vd = self.valuedecoder
        for key, value in self._items():
            yield (kd(key), vd(value))

    def items_from(self, key):
        fromkey = self.keycoder(key)
        kd = self.keydecoder
        vd = self.valuedecoder
        for key, value in self._items_from(fromkey):
            yield (kd(key), vd(value))

    def keys(self):
        kd = self.keydecoder
        for k in self._keys():
            yield kd(k)

    def keys_from(self, key):
        kd = self.keydecoder
        for k in self._keys_from(self.keycoder(key)):
            yield kd(k)
            
    def range_for_key(self, key):
        return self._range_for_key(self.keycoder(key))


class TermIndexWriter(CodedOrderedWriter):
    def __init__(self, dbfile):
        super(TermIndexWriter, self).__init__(dbfile)
        self.fieldcounter = 0
        self.fieldmap = {}
    
    def keycoder(self, key):
        # Encode term
        fieldmap = self.fieldmap
        fieldname, text = key
        
        if fieldname in fieldmap:
            fieldnum = fieldmap[fieldname]
        else:
            fieldnum = self.fieldcounter
            fieldmap[fieldname] = fieldnum
            self.fieldcounter += 1
        
        key = pack_ushort(fieldnum) + utf8encode(text)[0]
        return key
    
    def valuecoder(self, terminfo):
        return terminfo.to_string()
        
    def close(self):
        self._write_hashes()
        dbfile = self.dbfile
        
        dbfile.write_uint(len(self.index))
        for n in self.index:
            dbfile.write_long(n)
        dbfile.write_pickle(self.fieldmap)
        
        self._write_directory()
        self.dbfile.close()


class TermIndexReader(CodedOrderedReader):
    def __init__(self, dbfile):
        super(TermIndexReader, self).__init__(dbfile)
        
        dbfile.seek(self.indexbase + self.length * _LONG_SIZE)
        self.fieldmap = dbfile.read_pickle()
        self.names = [None] * len(self.fieldmap)
        for name, num in self.fieldmap.iteritems():
            self.names[num] = name
    
    def keycoder(self, key):
        fieldname, text = key
        fnum = self.fieldmap.get(fieldname, 65535)
        return pack_ushort(fnum) + utf8encode(text)[0]
        
    def keydecoder(self, v):
        return (self.names[unpack_ushort(v[:2])[0]], utf8decode(v[2:])[0])
    
    def valuedecoder(self, v):
        return TermInfo.from_string(v)
    
    def items_from(self, key):
        fromkey = self.keycoder(key)
        kd = self.keydecoder
        vd = self.valuedecoder
        for key, value in self._items_from(fromkey):
            yield (kd(key), vd(value))
            
    def terms_and_freqs(self, fromkey=None):
        dbfile = self.dbfile
        read = self.read
        kd = self.keydecoder
        
        if fromkey:
            gen = self._ranges_from(self.keycoder(fromkey))
        else:
            gen = self._ranges()
        
        for keypos, keylen, datapos, datalen in gen:
            yield (kd(read(keypos, keylen)),
                   (TermInfo.read_frequency(dbfile, datapos),
                    TermInfo.read_doc_freq(dbfile, datapos)))
    
    def frequency(self, key):
        datapos = self.range_for_key(key)[0]
        return TermInfo.read_frequency(self.dbfile, datapos)
    
    def doc_frequency(self, key):
        datapos = self.range_for_key(key)[0]
        return TermInfo.read_doc_freq(self.dbfile, datapos)
    
    def min_length(self, key):
        datapos = self.range_for_key(key)[0]
        return TermInfo.read_min_and_max_length(self.dbfile, datapos)[0]
    
    def max_length(self, key):
        datapos = self.range_for_key(key)[0]
        return TermInfo.read_min_and_max_length(self.dbfile, datapos)[1]
    
    def max_weight(self, key):
        datapos = self.range_for_key(key)[0]
        return TermInfo.read_max_weight(self.dbfile, datapos)
    
    def max_wol(self, key):
        datapos = self.range_for_key(key)[0]
        return TermInfo.read_max_wol(self.dbfile, datapos)
            

# docnum, fieldnum
_vectorkey_struct = Struct("!IH")


class TermVectorWriter(TermIndexWriter):
    def keycoder(self, key):
        fieldmap = self.fieldmap
        docnum, fieldname = key
        
        if fieldname in fieldmap:
            fieldnum = fieldmap[fieldname]
        else:
            fieldnum = self.fieldcounter
            fieldmap[fieldname] = fieldnum
            self.fieldcounter += 1
        
        return _vectorkey_struct.pack(docnum, fieldnum)
    
    def valuecoder(self, offset):
        return pack_long(offset)
        

class TermVectorReader(TermIndexReader):
    def keycoder(self, key):
        return _vectorkey_struct.pack(key[0], self.fieldmap[key[1]])
        
    def keydecoder(self, v):
        docnum, fieldnum = _vectorkey_struct.unpack(v)
        return (docnum, self.names[fieldnum])
    
    def valuedecoder(self, v):
        return unpack_long(v)[0]
    

class LengthWriter(object):
    def __init__(self, dbfile, doccount, lengths=None):
        self.dbfile = dbfile
        self.doccount = doccount
        if lengths is not None:
            self.lengths = lengths
        else:
            self.lengths = {}
    
    def add_all(self, items):
        lengths = self.lengths
        for docnum, fieldname, byte in items:
            if byte:
                if fieldname not in lengths:
                    lengths[fieldname] = array("B", (0 for _ in xrange(self.doccount)))
                lengths[fieldname][docnum] = byte
    
    def add(self, docnum, fieldname, byte):
        lengths = self.lengths
        if byte:
            if fieldname not in lengths:
                lengths[fieldname] = array("B", (0 for _ in xrange(self.doccount)))
            lengths[fieldname][docnum] = byte
    
    def reader(self):
        return LengthReader(None, self.doccount, lengths=self.lengths)
    
    def close(self):
        self.dbfile.write_ushort(len(self.lengths))
        for fieldname, arry in self.lengths.iteritems():
            self.dbfile.write_string(fieldname)
            self.dbfile.write_array(arry)
        self.dbfile.close()
        

class LengthReader(object):
    def __init__(self, dbfile, doccount, lengths=None):
        self.doccount = doccount
        
        if lengths is not None:
            self.lengths = lengths
        else:
            self.lengths = {}
            count = dbfile.read_ushort()
            for _ in xrange(count):
                fieldname = dbfile.read_string()
                self.lengths[fieldname] = dbfile.read_array("B", self.doccount)
            dbfile.close()
    
    def __iter__(self):
        for fieldname in self.lengths.keys():
            for docnum, byte in enumerate(self.lengths[fieldname]):
                yield docnum, fieldname, byte
    
    def get(self, docnum, fieldname, default=0):
        lengths = self.lengths
        if fieldname not in lengths:
            return default
        byte = lengths[fieldname][docnum] or default
        return byte_to_length(byte)
        

_stored_pointer_struct = Struct("!qI")  # offset, length
stored_pointer_size = _stored_pointer_struct.size
pack_stored_pointer = _stored_pointer_struct.pack
unpack_stored_pointer = _stored_pointer_struct.unpack


class StoredFieldWriter(object):
    def __init__(self, dbfile, fieldnames):
        self.dbfile = dbfile
        self.length = 0
        self.directory = []
        
        self.dbfile.write_long(0)
        self.dbfile.write_uint(0)
        
        self.name_map = {}
        for i, name in enumerate(fieldnames):
            self.name_map[name] = i
    
    def append(self, values):
        f = self.dbfile
        
        name_map = self.name_map
        
        vlist = [None] * len(name_map)
        for k, v in values.iteritems():
            if k in name_map:
                vlist[name_map[k]] = v
            else:
                # For dynamic stored fields, put them at the end of the list
                # as a tuple of (fieldname, value)
                vlist.append((k, v))
                
        v = dumps(vlist, -1)[2:-1]
        self.length += 1
        self.directory.append(pack_stored_pointer(f.tell(), len(v)))
        f.write(v)
    
    def close(self):
        f = self.dbfile
        directory_pos = f.tell()
        f.write_pickle(self.name_map)
        for pair in self.directory:
            f.write(pair)
        f.flush()
        f.seek(0)
        f.write_long(directory_pos)
        f.write_uint(self.length)
        f.close()


class StoredFieldReader(object):
    def __init__(self, dbfile):
        self.dbfile = dbfile

        dbfile.seek(0)
        pos = dbfile.read_long()
        self.length = dbfile.read_uint()
        
        dbfile.seek(pos)
        name_map = dbfile.read_pickle()
        self.names = [None] * len(name_map)
        for name, pos in name_map.iteritems():
            self.names[pos] = name
        self.directory_offset = dbfile.tell()
        
    def close(self):
        self.dbfile.close()

    def __getitem__(self, num):
        if num > self.length - 1:
            raise IndexError("Tried to get document %s, file has %s" % (num, self.length))
        
        dbfile = self.dbfile
        start = self.directory_offset + num * stored_pointer_size
        dbfile.seek(start)
        ptr = dbfile.read(stored_pointer_size)
        if len(ptr) != stored_pointer_size:
            raise Exception("Error reading %r @%s %s < %s" % (dbfile, start, len(ptr), stored_pointer_size))
        position, length = unpack_stored_pointer(ptr)
        vlist = loads(dbfile.map[position:position + length] + ".")
        
        names = self.names
        # Recreate a dictionary by putting the field names and values back
        # together by position. We can't just use dict(zip(...)) because we
        # want to filter out the None values.
        values = dict((names[i], vlist[i]) for i in xrange(len(names))
                      if vlist[i] is not None)
        
        # Pull out an extra stored dynamic field values off the end of the list
        if len(vlist) > len(names):
            values.update(dict(vlist[len(names):]))
        
        return values


# TermInfo

class TermInfo(object):
    struct = Struct("!fIBBff")
    
    def __init__(self, weight=0.0, docfreq=0, minlength=None, maxlength=0,
                 maxweight=0.0, maxwol=0.0, postings=None):
        self._weight = weight
        self._docfreq = docfreq
        self._minlength = minlength  # (as byte)
        self._maxlength = maxlength  # (as byte)
        self._maxweight = maxweight
        self._maxwol = maxwol
        self.postings = postings
    
    def frequency(self):
        return self._weight
    
    def doc_frequency(self):
        return self._docfreq
    
    def min_length(self):
        return byte_to_length(self._minlength)
    
    def max_length(self):
        return byte_to_length(self._maxlength)
    
    def max_weight(self):
        return self._maxweight
    
    def max_wol(self):
        return self._maxwol
    
    def add_block(self, block):
        self._weight += sum(block.weights)
        self._docfreq += len(block)
        
        ml = length_to_byte(block.min_length())
        if self._minlength is None:
            self._minlength = ml
        else:
            self._minlength = min(self._minlength, ml)
        
        xl = length_to_byte(block.max_length())
        self._maxlength = max(self._maxlength, xl)
        
        self._maxweight = max(self._maxweight, block.max_weight())
        self._maxwol = max(self._maxwol, block.max_wol())
    
    def to_string(self):
        ml = length_to_byte(self._minlength)
        xl = length_to_byte(self._maxlength)
        st = self.struct.pack(self._weight, self._docfreq, ml, xl,
                              self._maxweight, self._maxwol)
        
        if isinstance(self.postings, tuple):
            magic = 1
            st += dumps(self.postings, -1)[2:-1]
        else:
            magic = 0
            p = -1 if self.postings is None else self.postings
            st += pack_long(p)
        return chr(magic) + st

    @classmethod
    def from_string(cls, s):
        hbyte = ord(s[0])
        if hbyte < 2:
            # Freq, Doc freq, min length, max length, max weight, max WOL
            f, df, ml, xl, xw, xwol = cls.struct.unpack(s[1:cls.struct.size+1])
            ml = byte_to_length(ml)
            xl = byte_to_length(xl)
            # Postings
            pstr = s[cls.struct.size + 1:]
            if hbyte == 0:
                p = unpack_long(pstr)[0]
            else:
                p = loads(pstr + ".")
        else:
            raise Exception("Unknown struct header %s" % hbyte)
        
        return cls(f, df, ml, xl, xw, xwol, p)
    
    @classmethod
    def read_frequency(cls, dbfile, datapos):
        return dbfile.get_float(datapos + 1)
    
    @classmethod
    def read_doc_freq(cls, dbfile, datapos):
        return dbfile.get_uint(datapos + 1 + _FLOAT_SIZE)
    
    @classmethod
    def read_min_and_max_length(cls, dbfile, datapos):
        lenpos = datapos + 1 + _FLOAT_SIZE + _INT_SIZE
        ml = byte_to_length(dbfile.get_byte(lenpos))
        xl = byte_to_length(dbfile.get_byte(lenpos + 1))
        return ml, xl
    
    @classmethod
    def read_max_weight(cls, dbfile, datapos):
        weightspos = datapos + 1 + _FLOAT_SIZE + _INT_SIZE + 2
        return dbfile.get_float(weightspos)
    
    @classmethod
    def read_max_wol(cls, dbfile, datapos):
        weightspos = datapos + 1 + _FLOAT_SIZE + _INT_SIZE + 2
        return dbfile.get_float(weightspos + _FLOAT_SIZE)
    

# Utility functions

#def dump_hash(hashreader):
#    dbfile = hashreader.dbfile
#    read = hashreader.read
#    eod = hashreader._start_of_hashes
#
#    print "HEADER_SIZE=", hashreader.header_size, "eod=", eod
#
#    # Dump hashtables
#    for bucketnum in xrange(256):
#        pos, numslots = unpack_header_entry(read(bucketnum * header_entry_size, header_entry_size))
#        if numslots:
#            print "Bucket %d: %d slots" % (bucketnum, numslots)
#
#            dbfile.seek(pos)
#            for _ in xrange(0, numslots):
#                print "  %X : %d" % hashreader.unpack_pointer(read(pos, pointer_size))
#                pos += pointer_size
#        else:
#            print "Bucket %d empty: %s, %s" % (bucketnum, pos, numslots)
#
#    # Dump keys and values
#    print "-----"
#    pos = hashreader.header_size
#    dbfile.seek(pos)
#    while pos < eod:
#        keylen, datalen = unpack_lengths(read(pos, lengths_size))
#        keypos = pos + lengths_size
#        datapos = pos + lengths_size + keylen
#        key = read(keypos, keylen)
#        data = read(datapos, datalen)
#        print "%d +%d,%d:%r->%r" % (pos, keylen, datalen, key, data)
#        pos = datapos + datalen


