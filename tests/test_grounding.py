"""The fact checker rejects any reworded reply that states something the backend did not."""

from __future__ import annotations

import unittest

from automotive import grounding

PAYLOAD = {
    'vehicle': {'make': 'HONDA', 'model': 'Accord', 'model_year': 2015},
    'cards': [
        {'product': {'product_code': 'GDB3520', 'brand': 'TRW', 'product_name': 'ხუნდი წინა'},
         'availability_status': 'IN_STORE', 'availability': 'In the store: 3'},
        {'product': {'product_code': 'P28076', 'brand': 'BREMBO', 'product_name': 'ხუნდი წნ'},
         'availability_status': 'WITHIN_1_2_HOURS', 'availability': 'In TUZ 11 stock: 2'},
    ],
}
DRAFT = ('I found 2 front brake pads for your 2015 Honda Accord. 1 is in the store now, and '
         '1 can be here from TUZ 11 in 1-2 hours.')


def check(text, language='en'):
    allowed = grounding.facts_text(PAYLOAD, ['front brake pads'])
    return grounding.problems(text, allowed, language, draft=DRAFT, max_questions=0)


class GroundingTests(unittest.TestCase):
    def test_a_faithful_rewording_passes(self):
        self.assertEqual(check('Good news: 2 front brake pads for your 2015 Honda Accord — 1 is '
                               'in the store now, and 1 can come from TUZ 11 in 1-2 hours.'), [])

    def test_invented_facts_are_rejected(self):
        cases = {
            'We have 4 front pads for your Accord in the store.': 'unknown_token:4',
            'Two Bosch sets for your Accord are in the store.': 'unknown_brand:bosch',
            'The pads cost 120 GEL each.': 'claim:price',
            'They will be delivered tomorrow.': 'claim:delivery',
            'The 2 pads arrive in 3 days.': 'claim:delivery',
            'These definitely fit your Accord.': 'claim:guarantee',
            'See https://example.com for the 2 pads.': 'claim:url',
            'Call us about the 2 pads for your Accord.': 'claim:contact',
            'The 2 pads suit your 2016 Accord.': 'unknown_token:2016',
            'Your Accord also takes the Akebono set.': 'unknown_brand:akebono',
            'Both pads come from Sumitomo for your Accord.': 'unknown_name:Sumitomo',
        }
        for text, problem in cases.items():
            self.assertIn(problem, check(text), text)

    def test_bypasses_found_in_review_are_rejected(self):
        cases = {
            'Seventeen pads are in the store for your Accord.': 'unknown_number:Seventeen',
            'Twelve pads are in the store for your Accord.': 'unknown_number:Twelve',
            'Both on sale today for your Accord.': 'claim:price',
            'Order at parts-shop.net for your Accord.': 'claim:url',
            'Message telegram @partsguy about your Accord.': 'claim:contact',
            'Hella makes 2 front pads for your Accord.': 'unknown_brand:hella',
            'Вrembo makes 2 pads for your Accord.': 'foreign_script',
            'They match your Accord exactly.': 'claim:guarantee',
            '٢ pads are in the store.': 'non_ascii_digit',
            'Kenwood has 2 front pads for your Accord.': 'unknown_name:Kenwood',
        }
        for text, problem in cases.items():
            self.assertIn(problem, check(text), text)
        allowed = grounding.facts_text(PAYLOAD)
        draft = 'თქვენი 2015 წლის Honda Accord-ისთვის ვიპოვე 2 წინა სამუხრუჭე ხუნდი.'
        for text, problem in {
            'Honda Accord-ისთვის ხუთ ხუნდს ვიპოვე.': 'unknown_number:ხუთ',
            'Honda Accord-ისთვის 2 ბრემბოს ხუნდი გვაქვს.': 'unknown_brand:ბრემბოს',
        }.items():
            self.assertIn(problem, grounding.problems(text, allowed, 'ka', draft=draft), text)
        self.assertEqual(grounding.problems('x' * 5000, allowed, 'en'), ['too_long'])

    def test_extra_questions_and_wrong_language_are_rejected(self):
        self.assertIn('too_many_questions', check('Want 2 pads for your Accord?'))
        self.assertIn('wrong_language', check('ვიპოვე 2 ხუნდი თქვენი Accord-ისთვის.', 'en'))
        self.assertIn('wrong_language', check('I found 2 pads for your Accord.', 'ka'))

    def test_georgian_rewording_with_fact_names_passes(self):
        text = ('თქვენი 2015 წლის Honda Accord-ისთვის 2 წინა ხუნდი ვიპოვე: 1 მაღაზიაშია, '
                '1 კი TUZ 11-დან 1–2 საათში ჩამოვა.')
        allowed = grounding.facts_text(PAYLOAD, ['წინა ხუნდები'])
        draft = ('თქვენი 2015 წლის Honda Accord-ისთვის ვიპოვე 2 წინა სამუხრუჭე ხუნდი. 1 ახლა '
                 'მაღაზიაშია, 1 TUZ 11-დან 1–2 საათში ჩამოვა.')
        self.assertEqual(grounding.problems(text, allowed, 'ka', draft=draft), [])

    def test_without_a_draft_only_claims_no_template_makes_are_rejected(self):
        allowed = grounding.facts_text(PAYLOAD)
        self.assertEqual(grounding.problems(DRAFT, allowed, 'en'), [])
        self.assertIn('claim:price', grounding.problems('Only 5 GEL.', allowed, 'en'))

    def test_digit_tokens_normalize_codes_and_ranges(self):
        self.assertEqual(grounding.digit_tokens('AP104/2 in 1–2 hours, 6,5 L'),
                         {'ap1042', '1-2', '6.5'})
        self.assertEqual(grounding.problems('Drive safe, your 2 pads are ready.',
                                            grounding.facts_text(PAYLOAD), 'en'), [])


if __name__ == '__main__':
    unittest.main()
