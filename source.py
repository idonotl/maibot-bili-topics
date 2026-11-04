import re
import httpx

URL='https://api.bilibili.com/x/web-interface/popular'
HEADERS={'User-Agent':'Mozilla/5.0','Referer':'https://www.bilibili.com/'}
def clean(value,limit):
    return ' '.join(re.sub(r'[\x00-\x1f\x7f]',' ',str(value or '')).split())[:limit]

def parse(payload,interests,now):
    if not isinstance(payload,dict) or payload.get('code') != 0: raise ValueError('B站接口拒绝或返回异常')
    data=payload.get('data') or {}; rows=data.get('list') or []
    if not isinstance(rows,list): raise ValueError('B站数据格式异常')
    terms=[str(x).strip().casefold() for x in interests if str(x).strip()]
    result=[]
    for x in rows[:20]:
        if not isinstance(x,dict): continue
        bvid=str(x.get('bvid') or '')
        if not re.fullmatch(r'BV[0-9A-Za-z]{10}',bvid): continue
        try: published=float(x.get('pubdate') or 0)
        except (ValueError,TypeError): continue
        if published<=0 or published>now+300 or published<now-14*86400: continue
        title=clean(x.get('title'),140);description=clean(x.get('desc'),400); category=clean(x.get('tname'),40)
        if not title or not terms or not any(t in (title+' '+description+' '+category).casefold() for t in terms): continue
        owner=x.get('owner') if isinstance(x.get('owner'),dict) else {}
        result.append({'bvid':bvid,'title':title,'description':description,'category':category,'author':clean(owner.get('name'),60),'url':'https://www.bilibili.com/video/'+bvid,'published':published,'fetched':now})
        if len(result)>=5: break
    return result

async def fetch(interests,now):
    async with httpx.AsyncClient(timeout=15,headers=HEADERS,trust_env=False,follow_redirects=False) as client:
        async with client.stream('GET',URL,params={'ps':20,'pn':1}) as response:
            response.raise_for_status()
            content=bytearray()
            async for part in response.aiter_bytes():
                content.extend(part)
                if len(content)>1024*1024: raise ValueError('B站响应过大')
    import json
    return parse(json.loads(content),interests,now)
