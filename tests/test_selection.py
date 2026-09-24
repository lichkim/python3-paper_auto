import itertools
import tempfile
import unittest
from pathlib import Path
from paper_trend.models import Paper, merge_papers
from paper_trend.selection import select_papers, load_policy, save_selection_audit
from paper_trend.config import Topic, load_journals
from paper_trend.scoring import score_paper
from datetime import date

class SelectionTests(unittest.TestCase):
    def setUp(self):
        self.topics=[Topic('transfer','quantum state transfer',('arxiv','semantic_scholar'))]
        self.journals=load_journals(Path(__file__).resolve().parent.parent/'journals.json')
    def paper(self, **kwargs):
        return Paper(**{'title':'Quantum state transfer in a spin chain','venue':'arXiv',
                       'sources':{'arxiv'},'topics':{'transfer'},**kwargs})
    def select(self, p):return select_papers([p],self.topics,self.journals)
    def test_whitelist_applies_to_semantic_and_arxiv_linked_journal(self):
        for sources in ({'semantic_scholar'},{'arxiv','semantic_scholar'}):
            kept,audit=self.select(self.paper(venue='RSC Advances',sources=sources,arxiv_id='2609.00001'))
            self.assertEqual(kept,[]);self.assertEqual(audit[0]['reason'],'허용 목록 밖 저널')
    def test_allowed_alias_and_topic_reason(self):
        kept,audit=self.select(self.paper(venue='Phys. Rev. X'))
        self.assertEqual(len(kept),1);score_paper(kept[0],None)
        self.assertTrue(any('제목' in s for s in kept[0].score_reasons))
    def test_semantic_unknown_venue_is_explicit(self):
        kept,_=self.select(self.paper(venue='Semantic Scholar',sources={'semantic_scholar'}))
        self.assertIn('미확인',kept[0].selection_reasons[0])
    def test_irrelevant_examples_rejected(self):
        for title in ['Insights into optical performance of red phosphors through Judd Ofelt analysis',
                      'SALTED a symmetry adapted machine learning program for predicting electron densities',
                      'Carbon quantum dot rhodamine hybrid emissive states']:
            self.assertEqual(self.select(self.paper(title=title))[0],[])
    def test_abstract_can_establish_topic(self):
        kept,_=self.select(self.paper(title='Efficient conversion of optical states',abstract='We demonstrate quantum state transfer between photons and phonons.'))
        self.assertEqual(len(kept),1)
    def test_disconnected_query_words_do_not_match(self):
        self.assertEqual(self.select(self.paper(title='Quantum materials',abstract='state '+('unrelated '*30)+'transfer'))[0],[])
    def test_stale_topic_routes_and_source_rejected(self):
        self.assertEqual(self.select(self.paper(topics={'removed-topic'}))[0],[])
        self.assertEqual(self.select(self.paper(sources={'crossref'}))[0],[])
    def test_duplicate_bridge_is_transitive_in_every_order(self):
        for order in itertools.permutations([0,1,2]):
            papers=[self.paper(title='A',doi='10.1/a'),self.paper(title='B',arxiv_id='2609.00001v1'),
                    self.paper(title='C',doi='https://doi.org/10.1/a',arxiv_id='2609.00001v2')]
            merged=merge_papers([papers[i] for i in order]);self.assertEqual(len(merged),1)
            self.assertTrue({'title:a','title:b','title:c'} <= merged[0].all_keys)
    def test_does_not_mutate_cached_input(self):
        p=self.paper(topics={'transfer','removed-topic'});self.select(p)
        self.assertEqual(p.topics,{'transfer','removed-topic'})
    def test_audit_preserves_rejection_reason_and_version(self):
        _,audit=self.select(self.paper(venue='RSC Advances'))
        with tempfile.TemporaryDirectory() as tmp:
            path=save_selection_audit(Path(tmp),date(2026,9,22),audit)
            text=path.read_text();self.assertIn('topic-journal-v1.0',text);self.assertIn('허용 목록 밖 저널',text)
