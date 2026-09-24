"""Final deterministic selection gate shared by live results and old caches."""
from __future__ import annotations
import copy
import json
import re
from pathlib import Path
from .models import normalize_title

POLICY_PATH = Path(__file__).with_name('selection_policy.json')


def load_policy():
    return json.loads(POLICY_PATH.read_text(encoding='utf-8'))


def topic_reason(title, abstract, query, policy):
    query = normalize_title(query)
    texts = [('제목', normalize_title(title)), ('초록', normalize_title(re.sub('<[^>]+>', ' ', abstract)))]
    phrases = policy['topic_phrases'].get(query, [])
    for field, text in texts:
        for phrase in phrases:
            if f' {normalize_title(phrase)} ' in f' {text} ':
                return f'{field} 주제 구문: {phrase}'
    # Require the full query in a local context, never a fraction of common words.
    terms = [w for w in query.split() if w not in {'the','a','an','of','for','in','and','or','with'}]
    if not terms:
        return ''
    singular = lambda word: word[:-1] if len(word)>3 and word.endswith('s') and not word.endswith('ss') else word
    terms = {singular(w) for w in terms}
    for field, text in texts:
        words = [singular(w) for w in text.split()]
        for i in range(len(words)):
            if terms <= set(words[i:i+policy['context_window_words']]):
                return f'{field} 근접 문맥: {query}'
    return ''


def select_papers(papers, topics, journals, policy=None):
    policy = policy or load_policy()
    allowed = {normalize_title(j.name) for j in journals}
    aliases = {normalize_title(k):normalize_title(v) for k,v in policy['journal_aliases'].items()}
    generic = {'', 'arxiv', 'semantic scholar', 'unknown'}
    topic_map = {t.name:t for t in topics}
    kept=[]; decisions=[]
    for original in papers:
        p=copy.deepcopy(original)
        venue = aliases.get(normalize_title(p.venue), normalize_title(p.venue))
        source_ok = bool(p.sources & {'arxiv','semantic_scholar','aps','nature'}) or ('crossref' in p.sources and venue in allowed)
        venue_ok = venue in allowed or (venue in generic and bool(p.sources & {'arxiv','semantic_scholar'}))
        accepted=set(); reasons=[]; rejected_topics=[]
        for name in sorted(p.topics):
            topic=topic_map.get(name)
            if topic is None:
                rejected_topics.append({'query':name,'reason':'현재 설정에 없는 주제/과거 캐시 경로'})
                continue
            reason = topic_reason(p.title,p.abstract,topic.query,policy)
            if reason:
                accepted.add(name); reasons.append(f'{topic.query}: {reason}')
            else:
                rejected_topics.append({'query':topic.query,'reason':'제목·초록에 근접한 주제 근거 부족'})
        status='accepted' if source_ok and venue_ok and accepted else 'excluded'
        reason = ('허용되지 않은 수집처' if not source_ok else '허용 목록 밖 저널' if not venue_ok else
                  '주제 근거 부족' if not accepted else '저널/수집처 및 주제 검사 통과')
        decisions.append({'key':p.key,'title':p.title,'venue':p.venue,'sources':sorted(p.sources),
                          'status':status,'reason':reason,'topic_reasons':reasons,'rejected_topics':rejected_topics,
                          'policy_version':policy['version']})
        if status=='accepted':
            p.topics=accepted
            label='저널 확인: '+p.venue if venue in allowed else '저널 미확인 / 사전공개 검색 결과'
            p.selection_reasons=[label,*reasons]
            kept.append(p)
    return kept,decisions


def save_selection_audit(reports_dir,target,decisions,channel='daily'):
    path=reports_dir/f'{target.isoformat()}-{channel}-selection.json'
    path.parent.mkdir(parents=True,exist_ok=True)
    data={'policy':load_policy(),'decisions':decisions}
    temporary=path.with_suffix('.json.tmp')
    temporary.write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8')
    temporary.replace(path)
    return path
