import json
import os
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import index_usage


class UsageTests(unittest.TestCase):
    def test_normalization(self):
        norm = index_usage.normalize_usage
        self.assertEqual(norm({}), (None, None, None, 0))
        self.assertEqual(norm({'prompt_tokens': 10, 'completion_tokens': 4, 'total_tokens': 14, 'prompt_tokens_details': {'cached_tokens': 8}, 'completion_tokens_details': {'reasoning_tokens': 3}}), (10, 4, 14, 1))
        self.assertEqual(norm({'input_tokens': 10, 'output_tokens': 4, 'cache_creation_input_tokens': 5, 'cache_read_input_tokens': 3}), (18, 4, 22, 1))
        self.assertEqual(norm({'input_tokens': 10, 'output_tokens': 4, 'total_tokens': 14, 'cache_read_input_tokens': 3}), (10, 4, 14, 1))
        self.assertEqual(norm({'promptTokenCount': 10, 'candidatesTokenCount': 4, 'thoughtsTokenCount': 3, 'totalTokenCount': 17}), (10, 7, 17, 1))
        self.assertEqual(norm({'promptTokenCount': 10, 'candidatesTokenCount': 4, 'thoughtsTokenCount': 3}), (10, 7, 17, 1))
        self.assertEqual(norm({'input_tokens': 0}), (0, None, None, 1))

    def test_index_incremental_and_archive(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'test.sqlite')
            db = sqlite3.connect(path)
            db.executescript('''CREATE TABLE requests(id INTEGER PRIMARY KEY,method TEXT,path TEXT,finished REAL);
                CREATE TABLE body_chunks(request_id INTEGER,direction TEXT,seq INTEGER,data BLOB);
                CREATE TABLE archive_records(source TEXT,content_sha256 TEXT,content_json TEXT);''')
            db.executemany('INSERT INTO requests VALUES(?,?,?,?)', [(1,'POST','/v1/chat/completions?stream=true',1), (2,'POST','/v1/messages',None), (3,'POST','/v0/management/test',2), (4,'POST','/v1beta/models/gemini:generateContent?key=x',3)])
            sse = b'data: {"model":"x","choices":[],"usage":{"prompt_tokens":2,"completion_tokens":3,"total_tokens":5}}\n\ndata: {"choices":[],"usage":{"prompt_tokens":2,"completion_tokens":3,"total_tokens":5}}\n\ndata: [DONE]\n\n'
            db.executemany('INSERT INTO body_chunks VALUES(?,?,?,?)', [(1,'response',1,sse[30:]), (1,'response',0,sse[:30]), (4,'response',0,b'{}')])
            archived = json.dumps({'input_tokens': 10, 'output_tokens': 5, 'cache_read_tokens': 4, 'cache_creation_tokens': 2, 'reasoning_tokens': 3})
            db.executemany('INSERT INTO archive_records VALUES(?,?,?)', [('sqlite_requests','same',archived), ('sqlite_requests','same',archived), ('sqlite_requests','missing','{}'), ('codex_jsonl','nested',json.dumps({'usage': {'input_tokens': 999}}))])
            db.commit()
            result = index_usage.index_once(path)
            self.assertEqual((result['indexed'], result['archive_examined'], result['archived_missing']), (2, 4, 1))
            self.assertEqual(db.execute('SELECT request_id,input_tokens,output_tokens,total_tokens,model,has_usage,finished FROM model_token_usage ORDER BY request_id').fetchall(), [(1,2,3,5,'x',1,1), (4,None,None,None,None,0,3)])
            self.assertEqual(db.execute('SELECT * FROM archived_token_usage ORDER BY content_sha256').fetchall(), [('missing',None,None,None,0), ('same',10,5,15,1)])
            self.assertEqual(index_usage.index_once(path)['indexed'], 0)
            db.execute('UPDATE requests SET finished=4 WHERE id=2')
            db.execute('INSERT INTO body_chunks VALUES(?,?,?,?)', (2,'response',0,b'{"usage":{"input_tokens":7,"output_tokens":1}}'))
            db.commit()
            self.assertEqual(index_usage.index_once(path)['indexed'], 1)
            self.assertEqual(db.execute('SELECT total_tokens FROM model_token_usage WHERE request_id=2').fetchone()[0], 8)
            db.close()

    def test_cap_and_archive_batches(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'test.sqlite')
            db = sqlite3.connect(path)
            db.executescript('CREATE TABLE requests(id INTEGER PRIMARY KEY,method TEXT,path TEXT,finished REAL); CREATE TABLE body_chunks(request_id INTEGER,direction TEXT,seq INTEGER,data BLOB); CREATE TABLE archive_records(source TEXT,content_sha256 TEXT,content_json TEXT);')
            db.execute("INSERT INTO requests VALUES(1,'POST','/v1/responses',1)")
            db.execute("INSERT INTO body_chunks VALUES(1,'response',0,?)", (b'{"usage":{"total_tokens":9}}',))
            db.executemany('INSERT INTO archive_records VALUES(?,?,?)', [('sqlite_requests', str(i), '{}') for i in range(1001)])
            db.commit()
            with patch.object(index_usage, 'MAX_RESPONSE_BYTES', 4):
                first = index_usage.index_once(path)
            self.assertTrue(first['archive_more'])
            self.assertEqual(first['archived_missing'], 1000)
            self.assertEqual(db.execute('SELECT total_tokens,has_usage FROM model_token_usage').fetchone(), (None,0))
            second = index_usage.index_once(path)
            self.assertEqual(second['archive_examined'], 1)
            self.assertEqual(second['archived_missing'], 1001)
            db.close()


if __name__ == '__main__':
    unittest.main()
