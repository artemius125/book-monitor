"""Bounded AI assessment; code validates evidence before reversible merges."""
import asyncio
import json
import re
import time
from rapidfuzz.distance import DamerauLevenshtein
from .ai import AIUnavailable
from .fetch import FetchError
from .grouping import suggestions, proposal_hash, merge, incompatible
from .ingest import norm, author_key


def spelling_name(left, right):
    a,b=norm(left).split(),norm(right).split()
    if not a or len(a)>5 or len(a)!=len(b):
        return False
    # Match unordered name tokens one-to-one, including compatible initials.
    def compatible(x,y):
        return x==y or (min(len(x),len(y))==1 and x[0]==y[0]) or (
            min(len(x),len(y))>=4 and DamerauLevenshtein.distance(x,y)<=1)
    def assign(tokens,remaining):
        if not tokens:
            return True
        return any(compatible(tokens[0],v) and assign(tokens[1:],remaining[:i]+remaining[i+1:])
                   for i,v in enumerate(remaining))
    # Initials alone do not identify a person: require a shared complete name token.
    return any(len(x)>=4 and x==y for x in a for y in b) and assign(a,b)


def spelling_pair(left,right):
    # All aliases must remain mutually compatible, preventing transitive typo drift.
    aliases=left["aliases"]+right["aliases"]
    for i,a in enumerate(aliases):
        for b in aliases[i+1:]:
            ta,tb=norm(a["title"]),norm(b["title"])
            title_ok=ta==tb or (min(len(ta),len(tb))>=10 and DamerauLevenshtein.distance(ta,tb)<=1)
            if not title_ok or not spelling_name(a["author"],b["author"]):
                return False
    return True


def validated_evidence(advice,sources):
    by_url={s["url"]:s["text"] for s in sources}
    return [e.model_dump() for e in advice.evidence
            if e.url in by_url and e.quote in by_url[e.url] and len(e.quote.strip())>=15]


def explicit_identity(evidence,left,right):
    # An actual quoted alias statement must name both complete author variants.
    markers=r"псевдоним|настоящ\w* имя|также извест\w*|другое имя|pen name|pseudonym|also known as"
    for e in evidence:
        quote=norm(e["quote"])
        if not re.search(markers,quote):
            continue
        if re.search(r"\b(?:не|not|no)\b",quote):
            continue
        for a in left["aliases"]:
            for b in right["aliases"]:
                if author_key(a["author"])==author_key(b["author"]):
                    continue
                if all(t in quote.split() for t in norm(a["author"]).split()+norm(b["author"]).split()):
                    # A pseudonym statement alone cannot establish alternative book titles.
                    if norm(a["title"])==norm(b["title"]):
                        return True
    return False


class AutoGrouper:
    def __init__(self,cfg,store,ai,fetcher):
        self.cfg,self.store,self.ai,self.fetcher=cfg,store,ai,fetcher
        self.lock=asyncio.Lock()
        self.running=False

    def save(self,pair,state,decision):
        self.store.execute("INSERT OR REPLACE INTO ai_group_decisions VALUES(?,?,?,?,?)",
                           (pair["id"],proposal_hash(pair),state,json.dumps(decision,ensure_ascii=False),time.time()))

    def pending(self):
        return [p for p in suggestions(self.store) if p["state"]=="pending"
                and p["left"]["provisional"] and p["right"]["provisional"]]

    async def run(self,job_id):
        if not self.cfg.ai_enabled or not self.cfg.ai_auto_grouping:
            return {"processed":0,"remaining":0,"enabled":False}
        async with self.lock:
            self.running=True
            processed=0
            try:
                for _ in range(max(1,self.cfg.grouping_batch_size)):
                    pairs=self.pending()
                    if not pairs:
                        break
                    pair=pairs[0]
                    try:
                        async with asyncio.timeout(110):
                            await self.assess(pair,job_id)
                    except (AIUnavailable,TimeoutError) as e:
                        reason=str(e) if isinstance(e,AIUnavailable) else "Превышен лимит времени AI-разбора"
                        self.save(pair,"deferred",{"decision":"uncertain","reason":reason,"evidence":[]})
                        self.store.event("group_ai_unavailable",reason)
                        # Stop this batch after an API/budget failure; no blind paid retries.
                        for remaining in self.pending():
                            self.save(remaining,"deferred",{"decision":"uncertain","reason":reason,"evidence":[]})
                        break
                    except ValueError as e:
                        self.save(pair,"uncertain",{"decision":"uncertain","reason":str(e),"evidence":[]})
                        self.store.event("group_ai_uncertain",str(e))
                    processed+=1
                return {"processed":processed,"remaining":len(self.pending()),"enabled":True}
            finally:
                self.running=False

    async def assess(self,pair,job_id):
        left,right=pair["left"],pair["right"]
        sources=[]
        errors=[]
        simple=spelling_pair(left,right)
        if not simple:
            # Read at most one existing source URL per card. No search or invented URLs.
            for b in (left,right):
                if not b["urls"]:
                    continue
                try:
                    page=await self.fetcher.fetch(b["urls"][0],"group-"+pair["id"]+"-"+b["id"])
                    if page.get("extracted"):
                        sources.append({"url":page["url"],"text":page["extracted"]["text"][:6000]})
                except FetchError as e:
                    errors.append({"url":b["urls"][0],"code":e.code})
        advice=await self.ai.analyze_group(pair,sources,job_id)
        current=next((p for p in suggestions(self.store) if p["id"]==pair["id"]),None)
        if not current or current["state"]!="pending" or proposal_hash(current)!=proposal_hash(pair):
            return  # Source data or an operator decision changed while awaiting AI.
        evidence=validated_evidence(advice,sources)
        result={**advice.model_dump(),"evidence":evidence,"source_errors":errors,"model":self.cfg.ai_model,
                "left_id":left["id"],"right_id":right["id"]}
        state="uncertain"
        reason=advice.reason
        threshold=.9 if advice.basis=="spelling" else .95
        if advice.confidence>=threshold and advice.decision=="same":
            grounded=(advice.basis=="spelling" and simple) or (
                advice.basis=="source" and explicit_identity(evidence,left,right))
            if grounded and not incompatible(left,right):
                target=merge(self.store,right["id"],left["id"],"AI: "+reason,
                             title=advice.title or left["title"],author=advice.author or left["author"])
                result["canonical_book_id"]=target
                state="merged"
            else:
                reason+="; автоматическое объединение не подтверждено проверкой исходных данных / цитат"
        elif advice.confidence>=.95 and advice.decision=="different":
            # Distinct complete names or valid source evidence support separation.
            full=all(all(len(t)>1 for t in norm(a["author"]).split()) and len(norm(a["author"]).split())>=2
                     for b in (left,right) for a in b["aliases"])
            if (full and not simple) or evidence:
                state="different"
        result["reason"]=reason
        self.save(pair,state,result)
        self.store.event("group_ai_"+state,reason,level="info" if state in ("merged","different") else "warning")
