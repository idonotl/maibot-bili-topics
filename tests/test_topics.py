import sys, unittest, importlib, tempfile
from types import ModuleType
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo
root=Path(__file__).resolve().parents[1]
pkg=ModuleType('bili_test');pkg.__path__=[str(root)];sys.modules[pkg.__name__]=pkg
source=importlib.import_module('bili_test.source');Store=importlib.import_module('bili_test.store').Store
NOW=datetime(2026,10,27,12,tzinfo=ZoneInfo('Asia/Shanghai')).timestamp()
class TopicTests(unittest.TestCase):
    def sample(self):
        return {'code':0,'data':{'list':[{'bvid':'BV1234567890','title':'电竞比赛','desc':'精彩比赛','tname':'游戏','owner':{'name':'UP'},'pubdate':NOW-100}]}}
    def test_parse_and_source_identity(self):
        rows=source.parse(self.sample(),['电竞'],NOW)
        self.assertEqual(len(rows),1); self.assertEqual(rows[0]['url'],'https://www.bilibili.com/video/BV1234567890')
        self.assertEqual(source.parse(self.sample(),['动漫'],NOW),[])
        with self.assertRaises(ValueError):source.parse({'code':-412},['电竞'],NOW)
    def test_limits_persist_and_selection_spends_the_shared_pool_entry(self):
        with tempfile.TemporaryDirectory() as tmp:
            s=Store(Path(tmp)/'db');s.start()
            self.assertTrue(s.reserve(NOW,180,3));s.finish(NOW,'ok',source.parse(self.sample(),['电竞'],NOW))
            s=Store(Path(tmp)/'db');self.assertFalse(s.reserve(NOW+1,180,3))
            self.assertIsNotNone(s.pick('private:1',NOW));self.assertIsNone(s.pick('private:1',NOW))
            self.assertIsNone(s.pick('group:2',NOW))
            quiet=datetime(2026,10,28,1,tzinfo=ZoneInfo('Asia/Shanghai')).timestamp()
            self.assertFalse(s.reserve(quiet,180,3))
if __name__=='__main__':unittest.main()
