import json
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Asia/Shanghai")

# 旧库升级：缺哪列补哪列，不动已有数据
EXTRA_COLUMNS = {
    "duration": "REAL DEFAULT 0",
    "evidence_level": "TEXT DEFAULT 'description'",
    "summary": "TEXT DEFAULT ''",
    "keypoints": "TEXT DEFAULT '[]'",
    "processed_at": "REAL DEFAULT 0",
}


class Store:
    def __init__(self, path):
        self.path = path

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def start(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""CREATE TABLE IF NOT EXISTS runs(at REAL NOT NULL, day TEXT NOT NULL, result TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS topics(bvid TEXT PRIMARY KEY,title TEXT,description TEXT,category TEXT,author TEXT,url TEXT,published REAL,fetched REAL);
              CREATE TABLE IF NOT EXISTS used(target TEXT,bvid TEXT,at REAL,PRIMARY KEY(target,bvid));
              CREATE INDEX IF NOT EXISTS used_bvid ON used(bvid);
              CREATE TABLE IF NOT EXISTS deep_reads(at REAL NOT NULL,bvid TEXT NOT NULL,ok INTEGER NOT NULL,detail TEXT);
              CREATE TABLE IF NOT EXISTS browse_runs(at REAL NOT NULL, day TEXT NOT NULL, result TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS shared_watches(
                bvid TEXT NOT NULL,cid INTEGER NOT NULL,revision INTEGER NOT NULL,
                evidence TEXT NOT NULL,updated_utc REAL NOT NULL,PRIMARY KEY(bvid,cid));
              CREATE TABLE IF NOT EXISTS scenes(bvid TEXT PRIMARY KEY,cid INTEGER DEFAULT 0,duration REAL DEFAULT 0,frames INTEGER DEFAULT 0,text TEXT DEFAULT '',hotspots TEXT DEFAULT '',created_utc REAL NOT NULL,updated_utc REAL NOT NULL);
              CREATE TABLE IF NOT EXISTS transcripts(bvid TEXT PRIMARY KEY,cid INTEGER DEFAULT 0,model TEXT DEFAULT '',language TEXT DEFAULT '',duration REAL DEFAULT 0,text TEXT DEFAULT '',chars INTEGER DEFAULT 0,created_utc REAL NOT NULL,updated_utc REAL NOT NULL);""")
            self._migrate_scenes()
            existing = {row[1] for row in db.execute("PRAGMA table_info(topics)")}
            for column, decl in EXTRA_COLUMNS.items():
                if column not in existing:
                    db.execute(f"ALTER TABLE topics ADD COLUMN {column} {decl}")

    def reserve(self, now, interval, daily_max):
        day = datetime.fromtimestamp(now, TZ).date().isoformat()
        if datetime.fromtimestamp(now, TZ).hour in (*range(0, 8), 23):
            return False
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            last = db.execute('SELECT MAX(at) FROM runs').fetchone()[0] or 0
            count = db.execute('SELECT COUNT(*) FROM runs WHERE day=?', (day,)).fetchone()[0]
            if now - last < interval * 60 or count >= daily_max:
                return False
            db.execute('INSERT INTO runs VALUES (?,?,?)', (now, day, '采集中'))
            return True

    def finish(self, now, result, topics):
        with self.connect() as db:
            db.execute('UPDATE runs SET result=? WHERE at=?', (result, now))
            for t in topics:
                db.execute('INSERT OR IGNORE INTO topics (bvid,title,description,category,author,url,published,fetched) VALUES (?,?,?,?,?,?,?,?)',
                           tuple(t[k] for k in ('bvid', 'title', 'description', 'category', 'author', 'url', 'published', 'fetched')))
            db.execute('DELETE FROM topics WHERE fetched<?', (now - 7 * 86400,))
            db.execute('DELETE FROM deep_reads WHERE at<?', (now - 7 * 86400,))

    def pick(self, target, now):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute(
                'SELECT * FROM topics t WHERE fetched>=? '
                'AND NOT EXISTS (SELECT 1 FROM used u WHERE u.bvid=t.bvid) '
                'ORDER BY fetched DESC, published DESC LIMIT 1', (now - 3 * 86400,),
            ).fetchone()
            if row is None:
                return None
            db.execute('INSERT INTO used VALUES (?,?,?)', (target, row['bvid'], now))
            return self.export(row)

    def cooldown_ok(self, now, minutes):
        if minutes <= 0:
            return True
        with self.connect() as db:
            last = db.execute('SELECT MAX(at) FROM deep_reads WHERE ok=1').fetchone()[0] or 0
        return now - last >= minutes * 60

    def pending_deep_read(self, now, limit):
        """返回还没有摘要、且仍在可选窗口内的话题行，用于采集后预热。"""
        if limit <= 0:
            return []
        with self.connect() as db:
            rows = db.execute(
                "SELECT * FROM topics WHERE fetched>=? AND (evidence_level IS NULL OR evidence_level='description') "
                "AND (summary IS NULL OR summary='') ORDER BY published DESC LIMIT ?",
                (now - 3 * 86400, limit)).fetchall()
        return [self.export(row) for row in rows]

    def log_deep_read(self, now, bvid, ok, detail):
        with self.connect() as db:
            db.execute('INSERT INTO deep_reads VALUES (?,?,?,?)', (now, bvid, 1 if ok else 0, str(detail)[:300]))

    def save_summary(self, bvid, video, summary, rendered, danmaku_count, now):
        with self.connect() as db:
            db.execute('UPDATE topics SET duration=?,evidence_level=?,summary=?,keypoints=?,processed_at=? WHERE bvid=?',
                       (video.get('duration') or 0, 'danmaku', rendered,
                        json.dumps(summary.get('keypoints') or [], ensure_ascii=False), now, bvid))

    def export(self, row):
        data = dict(row)
        points = data.get('keypoints')
        try:
            data['keypoints'] = json.loads(points) if points else []
        except (TypeError, ValueError):
            data['keypoints'] = []
        data['evidence_level'] = data.get('evidence_level') or 'description'
        if data['evidence_level'] == 'danmaku':
            data['evidence'] = '基于该视频公开弹幕与简介的摘要'
        else:
            data['evidence'] = '仅标题、简介与分区'
        return data

    def recent(self, now, days=3, limit=5, keyword=""):
        """读取最近积攒的话题（含摘要），供“今天B站有什么热点”这类问题使用。"""
        limit = max(1, min(int(limit or 5), 10))
        with self.connect() as db:
            sql = "SELECT * FROM topics WHERE fetched>=?"
            args = [now - days * 86400]
            if keyword:
                sql += " AND (title LIKE ? OR description LIKE ?)"
                args += ["%%%s%%" % keyword, "%%%s%%" % keyword]
            sql += " ORDER BY published DESC LIMIT ?"
            args.append(limit)
            rows = db.execute(sql, args).fetchall()
        return [self.export(row) for row in rows]

    def reserve_browse(self, now, interval, daily_max):
        """主动浏览的独立配额：与定时采集互不影响。"""
        day = datetime.fromtimestamp(now, TZ).date().isoformat()
        if daily_max <= 0:
            return False
        if datetime.fromtimestamp(now, TZ).hour in (*range(0, 8), 23):
            return False
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            last = db.execute('SELECT MAX(at) FROM browse_runs').fetchone()[0] or 0
            count = db.execute('SELECT COUNT(*) FROM browse_runs WHERE day=?', (day,)).fetchone()[0]
            if now - last < interval * 60 or count >= daily_max:
                return False
            db.execute('INSERT INTO browse_runs VALUES (?,?,?)', (now, day, '浏览中'))
            return True

    def finish_browse(self, now, result, topics):
        with self.connect() as db:
            db.execute('UPDATE browse_runs SET result=? WHERE at=?', (result, now))
            for t in topics:
                db.execute('INSERT OR IGNORE INTO topics (bvid,title,description,category,author,url,published,fetched) '
                           'VALUES (?,?,?,?,?,?,?,?)',
                           tuple(t[k] for k in ('bvid', 'title', 'description', 'category',
                                                'author', 'url', 'published', 'fetched')))

    def consume_topics(self, limit=3):
        """取出积攒的话题并从池子里删掉，避免重复报同样的内容。"""
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute(
                "SELECT * FROM topics t WHERE summary IS NOT NULL AND summary<>'' "
                "AND NOT EXISTS (SELECT 1 FROM used u WHERE u.bvid=t.bvid) "
                "ORDER BY fetched DESC, published DESC LIMIT ?", (max(1, min(int(limit), 6)),)).fetchall()
            items = [self.export(row) for row in rows]
            for row in rows:
                db.execute("INSERT INTO used VALUES (?,?,?)", ("manual", row["bvid"], time.time()))
                db.execute("DELETE FROM topics WHERE bvid=?", (row["bvid"],))
        return items

    def _migrate_scenes(self) -> None:
        """给旧库补 danmaku 列。"""
        with self.connect() as db:
            columns = {row[1] for row in db.execute("PRAGMA table_info(scenes)")}
            if "danmaku" not in columns:
                db.execute("ALTER TABLE scenes ADD COLUMN danmaku TEXT DEFAULT ''")

    def save_scene(self, bvid, cid, duration, frames, text, hotspots, now, danmaku=''):
        with self.connect() as db:
            db.execute('INSERT OR REPLACE INTO scenes (bvid,cid,duration,frames,text,hotspots,danmaku,created_utc,updated_utc) '
                       'VALUES (?,?,?,?,?,?,?,?,?)',
                       (bvid, cid, duration, frames, text, hotspots, danmaku, now, now))

    def get_scene(self, bvid, max_age_days=30):
        with self.connect() as db:
            return db.execute('SELECT * FROM scenes WHERE bvid=? AND updated_utc>=?',
                              (bvid, __import__('time').time() - max_age_days * 86400)).fetchone()

    def get_transcript(self, bvid):
        with self.connect() as db:
            row = db.execute("SELECT * FROM transcripts WHERE bvid=?", (bvid,)).fetchone()
        return dict(row) if row else None

    def get_shared_watch(self, bvid, cid, revision):
        with self.connect() as db:
            row = db.execute("SELECT evidence FROM shared_watches WHERE bvid=? AND cid=? "
                             "AND revision=? AND updated_utc>=?",
                             (bvid, cid, revision, time.time() - 7 * 86400)).fetchone()
        return json.loads(row[0]) if row else None

    def save_shared_watch(self, bvid, cid, revision, evidence):
        now = time.time()
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO shared_watches VALUES (?,?,?,?,?)",
                       (bvid, cid, revision, json.dumps(evidence, ensure_ascii=False), now))
            db.execute("DELETE FROM shared_watches WHERE updated_utc < ?", (now - 7 * 86400,))
            db.execute("DELETE FROM shared_watches WHERE rowid IN "
                       "(SELECT rowid FROM shared_watches ORDER BY updated_utc DESC LIMIT -1 OFFSET 128)")

    def save_transcript(self, bvid, cid, model, language, duration, text):
        now = time.time()
        with self.connect() as db:
            db.execute(
                """INSERT INTO transcripts(bvid, cid, model, language, duration, text, chars, created_utc, updated_utc)
                   VALUES(?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(bvid) DO UPDATE SET cid=excluded.cid, model=excluded.model,
                   language=excluded.language, duration=excluded.duration, text=excluded.text,
                   chars=excluded.chars, updated_utc=excluded.updated_utc""",
                (bvid, int(cid or 0), model or "", language or "", float(duration or 0),
                 text or "", len(text or ""), now, now))

    def cleanup_transcripts(self, keep_days):
        with self.connect() as db:
            return db.execute("DELETE FROM transcripts WHERE updated_utc<?",
                              (time.time() - keep_days * 86400,)).rowcount

    def status(self):
        with self.connect() as db:
            row = db.execute('SELECT result FROM runs ORDER BY at DESC LIMIT 1').fetchone()
            topics = [self.export(x) for x in db.execute('SELECT * FROM topics WHERE fetched>=? ORDER BY fetched DESC, published DESC LIMIT 12', (time.time() - 3 * 86400,))]
            deep = db.execute('SELECT COUNT(*) FROM topics WHERE evidence_level=?', ('danmaku',)).fetchone()[0]
            try:
                last_deep = db.execute('SELECT at,ok,detail FROM deep_reads ORDER BY at DESC LIMIT 1').fetchone()
            except sqlite3.OperationalError:
                last_deep = None
        return {
            'last_result': row[0] if row else '尚未采集',
            'count': len(topics),
            'topics': topics,
            'deep_read_topics': deep,
            'last_deep_read': ({'at': last_deep[0], 'ok': bool(last_deep[1]), 'detail': last_deep[2]} if last_deep else None),
        }
