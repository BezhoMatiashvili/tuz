"""Deterministic understanding of small talk, typed cars, short answers and references."""

from __future__ import annotations

import unittest

from automotive import dialogue


class SmallTalkTests(unittest.TestCase):
    def test_kinds_in_english_georgian_and_latin_georgian(self):
        cases = {
            'hi there': 'greeting', 'Hello!': 'greeting', 'გამარჯობათ': 'greeting',
            'gamarjoba': 'greeting', 'thanks!': 'thanks', 'დიდი მადლობა': 'thanks',
            'madloba': 'thanks', 'ok': 'ack', 'კარგი': 'ack', 'ok bye': 'bye',
            'ნახვამდის': 'bye', 'are you a bot?': 'identity', 'ვინ ხარ?': 'identity',
            'რა შეგიძლია?': 'help', 'yes please': 'yes', 'კი': 'yes', 'no thanks': 'thanks',
            'არა': 'no',
        }
        for text, kind in cases.items():
            self.assertEqual(dialogue.small_talk(text), kind, text)

    def test_a_request_is_never_small_talk(self):
        for text in ('hello, I need an air filter', 'front pads', 'thanks, now rear pads',
                     '2015', 'AP104/2', 'ok but what about discs', 'კი, უკანაც'):
            self.assertIsNone(dialogue.small_talk(text), text)

    def test_leading_greeting(self):
        self.assertTrue(dialogue.starts_with_greeting('Hello, I need an air filter'))
        self.assertFalse(dialogue.starts_with_greeting('Hold on, front pads'))


class LanguageTests(unittest.TestCase):
    def test_codes_vins_and_ok_keep_the_conversation_language(self):
        self.assertEqual(dialogue.detect_language('AP104/2', 'ka'), 'ka')
        self.assertEqual(dialogue.detect_language('1HGCM82633A004352', 'en'), 'en')
        self.assertEqual(dialogue.detect_language('ok', 'ka'), 'ka')
        self.assertEqual(dialogue.detect_language('Find AP104/2', 'ka'), 'en')
        self.assertEqual(dialogue.detect_language('წინა ხუნდები', 'en'), 'ka')
        self.assertEqual(dialogue.detect_language('xundebi minda', 'en'), 'ka')
        self.assertEqual(dialogue.detect_language('2015'), 'ka')


class VehicleTextTests(unittest.TestCase):
    def check(self, text, make, model, year):
        typed = dialogue.read_vehicle(text)
        self.assertEqual((typed.make, typed.model, typed.model_year), (make, model, year), text)

    def test_make_model_and_year_in_both_languages(self):
        self.check('Honda Accord 2015', 'HONDA', 'Accord', 2015)
        self.check('2015 წლის ჰონდა აკორდი', 'HONDA', 'Accord', 2015)
        self.check('front brake pads for Honda Accord 2015', 'HONDA', 'Accord', 2015)
        self.check('Mercedes E 400 2015 oil', 'MERCEDES-BENZ', 'E-Class', 2015)
        self.check('bmw x5 2012', 'BMW', 'X5', 2012)

    def test_a_mercedes_designation_is_the_class_and_the_trim(self):
        cases = {
            'Mercedes Benz E400 2015 ზეთის შეცვლა მინდა': ('E-Class', 'E 400', 2015),
            'Mercedes E 400 2015 oil': ('E-Class', 'E 400', 2015),
            'Mercedes-Benz E-Class E400 2015': ('E-Class', 'E 400', 2015),
            'mercedes-benz GLE350d 2017': ('GLE-Class', 'GLE 350d', 2017),
            'Mercedes ML350 2012': ('M-Class', 'ML 350', 2012),
            'მერსედესი C200 2014': ('C-Class', 'C 200', 2014),
        }
        for text, (model, trim, year) in cases.items():
            typed = dialogue.read_vehicle(text)
            self.assertEqual((typed.make, typed.model, typed.trim, typed.model_year),
                             ('MERCEDES-BENZ', model, trim, year), text)
        self.assertEqual(dialogue.read_vehicle('Mercedes Benz E400 2015').as_vehicle()['trim'],
                         'E 400')

    def test_designations_of_other_makes_are_the_model_and_the_trim(self):
        cases = {
            'BMW 328i 2014': ('BMW', '3 Series', '328i'),
            'bmw 3 series 328i 2014': ('BMW', '3 Series', '328i'),
            'BMW 520d 2015': ('BMW', '5 Series', '520d'),
            'BMW M340i 2020': ('BMW', '3 Series', 'M340i'),
            'Lexus RX350 2015': ('LEXUS', 'RX', 'RX 350'),
            'lexus rx 450h 2016': ('LEXUS', 'RX', 'RX 450h'),
            'Lexus IS250 2012': ('LEXUS', 'IS', 'IS 250'),
            'Infiniti G37 2011': ('INFINITI', 'G', 'G 37'),
        }
        for text, (make, model, trim) in cases.items():
            typed = dialogue.read_vehicle(text)
            self.assertEqual((typed.make, typed.model, typed.trim), (make, model, trim), text)
        # Model names stay models.
        for text, (make, model) in {'BMW X5 2015': ('BMW', 'X5'),
                                    'Infiniti QX60 2016': ('INFINITI', 'QX60'),
                                    'Lexus RX 2015': ('LEXUS', 'RX')}.items():
            typed = dialogue.read_vehicle(text)
            self.assertEqual((typed.make, typed.model, typed.trim), (make, model, None), text)

    def test_two_word_and_joined_model_names(self):
        self.check('Range Rover Sport 2015', 'LAND ROVER', 'Range Rover Sport', 2015)
        self.check('Land Rover Range Rover Evoque 2016', 'LAND ROVER', 'Range Rover Evoque',
                   2016)
        self.check('range rover 2014 oil change', 'LAND ROVER', 'Range Rover', 2014)
        self.check('Land Rover Discovery 2015', 'LAND ROVER', 'Discovery', 2015)
        self.check('Toyota Land Cruiser 2015', 'TOYOTA', 'Land Cruiser', 2015)
        self.check('land cruiser 2015', 'TOYOTA', 'Land Cruiser', 2015)
        self.check('Santa Fe 2014', 'HYUNDAI', 'Santa Fe', 2014)
        self.check('mazda3 2015', 'MAZDA', '3', 2015)
        self.check('cx-5 2016', 'MAZDA', 'CX-5', 2016)

    def test_a_second_name_of_the_make_is_not_the_model(self):
        self.check('Mercedes Benz 2015', 'MERCEDES-BENZ', None, 2015)
        self.check('Mercedes-Benz E-Class 2015', 'MERCEDES-BENZ', 'E-Class', 2015)
        self.assertIsNone(dialogue.read_vehicle('Mercedes-Benz E-Class 2015').trim)
        self.assertEqual(dialogue.part_codes('Mercedes Benz E400 2015 oil change'), [])

    def test_a_known_model_names_its_make(self):
        self.check('air filter for my Accord 2015', 'HONDA', 'Accord', 2015)
        self.check('პრიუსის 2012 სალონის ფილტრი', 'TOYOTA', 'Prius', 2012)

    def test_partial_and_false_positive_cars(self):
        self.check('oil filter for a honda', 'HONDA', None, None)
        self.check('will it fit?', None, None, None)
        self.check('ფიტინგი მჭირდება', None, None, None)  # A fitting, not a Honda Fit.
        self.check('მანქანას აქვს ხმაური', None, None, None)  # "Has", not a Toyota Aqua.
        self.check('front pads', None, None, None)

    def test_short_answers(self):
        self.assertEqual(dialogue.year_answer('2015'), 2015)
        self.assertEqual(dialogue.year_answer("it's a 2015"), 2015)
        self.assertIsNone(dialogue.year_answer('2015 front pads'))
        self.assertEqual(dialogue.engine_answer('K24W'), 'K24W')
        self.assertEqual(dialogue.engine_answer('M 276'), 'M 276')
        self.assertIsNone(dialogue.engine_answer('2015'))
        self.assertIsNone(dialogue.engine_answer('front pads'))
        self.assertIsNone(dialogue.engine_answer('3.0L'))  # A size, not an engine code.

    def test_engine_size_answers(self):
        self.assertEqual(dialogue.displacement_answer('3.0 2 ტურბინიანი ძრავია'), '3.0')
        self.assertEqual(dialogue.displacement_answer('3.0L twin turbo'), '3.0')
        self.assertEqual(dialogue.displacement_answer('2,0 ლიტრი'), '2.0')
        self.assertIsNone(dialogue.displacement_answer('M276'))
        self.assertIsNone(dialogue.displacement_answer('3.0 or 3.5'))
        self.assertIsNone(dialogue.displacement_answer('3.0 2015'))

    def test_continuation_keeps_the_request_for_another_car(self):
        self.assertTrue(dialogue.continues('and for this one?'))
        self.assertTrue(dialogue.continues('და ამისთვის?'))
        self.assertFalse(dialogue.continues('1HGCM82633A004352'))


class CodeTests(unittest.TestCase):
    def test_years_grades_quantities_and_car_names_are_not_part_codes(self):
        self.assertEqual(dialogue.part_codes('front brake pads for Honda Accord 2015'), [])
        self.assertEqual(dialogue.part_codes('5W-40 oil, 4L'), [])
        self.assertEqual(dialogue.part_codes('bmw x5 2012 air filter'), [])
        self.assertEqual(dialogue.part_codes('Do you have AP104/2?'), ['AP104/2'])
        self.assertEqual(dialogue.part_codes('engine code B48', ('B48',)), [])

    def test_only_a_plain_code_question_skips_the_read_tools(self):
        self.assertTrue(dialogue.code_question('Do you have AP104/2?', ['AP104/2']))
        self.assertTrue(dialogue.code_question('AP104/2 გაქვთ?', ['AP104/2']))
        self.assertTrue(dialogue.code_question('front brake pads GDB3520', ['GDB3520']))
        self.assertFalse(dialogue.code_question('cross reference for AP104/2', ['AP104/2']))
        self.assertFalse(dialogue.code_question('does AP104/2 fit a 2013 Car?', ['AP104/2']))

    def test_grades_are_normalized(self):
        self.assertEqual(dialogue.grades('do you have 5w40 or 0W-20?'), ['5W-40', '0W-20'])


class ReferenceTests(unittest.TestCase):
    cards = [{'id': 'a', 'brand': 'TRW', 'code': 'GDB3520'},
             {'id': 'b', 'brand': 'BREMBO', 'code': 'P28076'}]

    def test_brand_and_ordinal_point_at_a_shown_card(self):
        self.assertEqual(dialogue.references('is the brembo one in stock?', self.cards), ['b'])
        self.assertEqual(dialogue.references('the second one', self.cards), ['b'])
        self.assertEqual(dialogue.references('პირველი მარაგშია?', self.cards), ['a'])
        self.assertEqual(dialogue.references('the last one', self.cards), ['b'])

    def test_no_reference_without_a_cue_or_cards(self):
        self.assertEqual(dialogue.references('rear pads', self.cards), [])
        self.assertEqual(dialogue.references('the second one', []), [])
        self.assertEqual(dialogue.references('მეორე მანქანისთვისაც მინდა', self.cards), [])
        self.assertEqual(dialogue.references('ბოლოს რა მირჩევთ?', self.cards), [])
        self.assertEqual(dialogue.references('for my second car', self.cards), [])
        self.assertEqual(dialogue.references('მეორეს რა ფასი აქვს?', self.cards), ['b'])


class ReviewRegressionTests(unittest.TestCase):
    def test_a_make_followed_by_a_part_number_is_not_a_model(self):
        typed = dialogue.read_vehicle('Toyota 04465-33450 do you have?')
        self.assertEqual((typed.make, typed.model), ('TOYOTA', None))
        self.assertEqual(dialogue.part_codes('Toyota 04465-33450 do you have?'),
                         ['04465-33450'])

    def test_a_vin_with_a_georgian_suffix_is_found_and_never_a_part_code(self):
        from automotive.chat import _redact

        text = 'მანქანა 1HGCM82633A004352ის ხუნდები'
        self.assertEqual(_redact(text), 'მანქანა [VIN]ის ხუნდები')
        self.assertEqual(dialogue.part_codes(text), [])


if __name__ == '__main__':
    unittest.main()
