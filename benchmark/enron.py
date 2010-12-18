from __future__ import division
import os.path, tarfile
from email import message_from_string
from marshal import dump, load
from urllib import urlretrieve
from zlib import compress, decompress

try:
    import xappy
except ImportError:
    pass

from whoosh import analysis, fields
from whoosh.support.bench import Bench
from whoosh.util import now


# Benchmark class

class EnronBench(Bench):
    enron_archive_url = "http://www.cs.cmu.edu/~enron/enron_mail_082109.tar.gz"
    enron_archive_filename = "enron_mail_082109.tar.gz"
    cache_filename = "enron_cache.pickle"

    header_to_field = {"Date": "date", "From": "frm", "To": "to",
                   "Subject": "subject", "Cc": "cc", "Bcc": "bcc"}

    _name = "enron"
    main_field = "body"
    headline_field = "subject"
    
    field_order = ("subject", "date", "from", "to", "cc", "bcc", "body")

    # Functions for downloading and then reading the email archive and caching
    # the messages in an easier-to-digest format
    
    def download_archive(self, archive):
        print "Downloading Enron email archive to %r..." % archive
        t = now()
        urlretrieve(self.enron_archive_url, archive)
        print "Downloaded in ", now() - t, "seconds"
        
    def get_texts(self, archive):
        archive = tarfile.open(archive, "r:gz")
        while True:
            entry = archive.next()
            archive.members = []
            if entry is None:
                break
            f = archive.extractfile(entry)
            if f is not None:
                text = f.read()
                yield text
    
    def get_messages(self, archive, headers=True):
        header_to_field = self.header_to_field
        for text in self.get_texts(archive):
            message = message_from_string(text)
            body = message.as_string().decode("latin_1")
            blank = body.find("\n\n")
            if blank > -1:
                body = body[blank+2:]
            d = {"body": body}
            if headers:
                for k in message.keys():
                    fn = header_to_field.get(k)
                    if not fn: continue
                    v = message.get(k).strip()
                    if v:
                        d[fn] = v.decode("latin_1")
            yield d
    
    def cache_messages(self, archive, cache):
        print "Caching messages in %s..." % cache
        
        if not os.path.exists(archive):
            raise Exception("Archive file %r does not exist" % archive)
        
        t = now()
        f = open(cache, "wb")
        c = 0
        for d in self.get_messages(archive):
            c += 1
            dump(d, f)
            if not c % 1000: print c
        f.close()
        print "Cached messages in ", now() - t, "seconds"

    def setup(self, options, args):
        archive = os.path.abspath(os.path.join(options.dir, self.enron_archive_filename))
        cache = os.path.abspath(os.path.join(options.dir, self.cache_filename))
    
        if not os.path.exists(archive):
            self.download_archive(archive)
        else:
            print "Archive is OK"
        
        if not os.path.exists(cache):
            self.cache_messages(archive, cache)
        else:
            print "Cache is OK"
    
    def documents(self):
        if not os.path.exists(self.cache_filename):
            raise Exception("Message cache does not exist, use --setup")
        
        f = open(self.cache_filename, "rb")
        try:
            while True:
                d = load(f)
                yield d
        except EOFError:
            pass
        f.close()
    
    def whoosh_schema(self):
        ana = analysis.StemmingAnalyzer(maxsize=40)
        schema = fields.Schema(body=fields.TEXT(analyzer=ana, stored=True),
                               date=fields.ID(stored=True),
                               frm=fields.ID(stored=True),
                               to=fields.IDLIST(stored=True),
                               subject=fields.TEXT(stored=True),
                               cc=fields.IDLIST,
                               bcc=fields.IDLIST)
        return schema

    def xappy_indexer_connection(self, path):
        conn = xappy.IndexerConnection(path)
        conn.add_field_action('body', xappy.FieldActions.INDEX_FREETEXT, language='en')
        conn.add_field_action('body', xappy.FieldActions.STORE_CONTENT)
        conn.add_field_action('date', xappy.FieldActions.INDEX_EXACT)
        conn.add_field_action('date', xappy.FieldActions.STORE_CONTENT)
        conn.add_field_action('frm', xappy.FieldActions.INDEX_EXACT)
        conn.add_field_action('frm', xappy.FieldActions.STORE_CONTENT)
        conn.add_field_action('to', xappy.FieldActions.INDEX_EXACT)
        conn.add_field_action('to', xappy.FieldActions.STORE_CONTENT)
        conn.add_field_action('subject', xappy.FieldActions.INDEX_FREETEXT, language='en')
        conn.add_field_action('subject', xappy.FieldActions.STORE_CONTENT)
        conn.add_field_action('cc', xappy.FieldActions.INDEX_EXACT)
        conn.add_field_action('bcc', xappy.FieldActions.INDEX_EXACT)
        return conn
    
    def process_document_whoosh(self, d):
        d["_stored_body"] = compress(d["body"], 9)
        
    def process_document_xapian(self, d):
        d[self.main_field] = " ".join([d.get(name, "") for name
                                       in self.field_order])
    


if __name__=="__main__":
    EnronBench().run()
        
    
