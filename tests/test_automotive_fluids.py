"""Fluid choosers: synthetic responses, no live claims in this module."""

from __future__ import annotations

import json
import unittest
from unittest.mock import Mock

from automotive.fuchs import FuchsClient
from automotive.jsfilter import JsFilterClient
from automotive.oats import OatsCatalog
from automotive.public_http import PublicSourceError, redact
from automotive.shell_oil import ShellOilClient
from tests.test_automotive_vin_search import VEHICLE, VIN, FederationTests

ONE_ROW = """
<table>
<tr class="model-title"><td colspan="9">HONDA</td></tr>
<tr>
  <td data-title="YEAR">1999-2003</td>
  <td data-title="ENG NO">D16B6</td>
  <td data-title="OIL"><a href="/catalogue/filter/1/C307J">C307J</a></td>
  <td data-title="AIR"><a href="/catalogue/filter/2/A867J">A867J</a></td>
</tr>
</table>
"""
TWO_ROWS = ONE_ROW.replace(
    '</table>',
    '<tr><td data-title="YEAR">2004</td><td data-title="ENG NO">K20</td>'
    '<td data-title="OIL"><a>C999J</a></td></tr></table>',
)


class FakeHttp:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def reserve(self):
        return None

    def read(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs.get('params'), kwargs.get('data')))
        status, body = self.responses.pop(0)
        if isinstance(body, bytes):
            return status, body
        return status, json.dumps(body).encode()


def equipment(identifier='eng1', displacement='2.0'):
    return {
        '@id': identifier, 'manufacturer': 'Example', 'model': 'Car 2.0',
        'display_name_long': f'Example Car {displacement}', 'yearfrom': 2012, 'yearto': 2014,
        'displacement': [displacement],
    }


class RoutedHttp:
    """Answers by address, so that variants read side by side get their own records."""

    def __init__(self, listed, details):
        self.listed, self.details, self.calls = listed, details, []

    def reserve(self):
        return None

    def read(self, method, url, **kwargs):
        self.calls.append(url)
        if url.endswith('/search'):
            return 200, json.dumps({'equipment_list': {'equipment': self.listed}}).encode()
        detail = self.details[url.rsplit('/', 1)[1]]
        if isinstance(detail, Exception):
            raise detail
        return 200, json.dumps({'equipment': detail}).encode()


def record(*grades):
    return {'display_name_long': 'Example Car', 'application': [{
        'name': 'Engine', 'display_capacity': '4.2',
        'product': [{'name': f'Helix Ultra {grade}', 'external_id': 'OIL1'} for grade in grades]}]}


class OatsVariantTests(unittest.TestCase):
    """Several engines: the chooser says which grades each takes, and picks none."""

    def lookup(self, listed, details):
        client = OatsCatalog('Shell', 'https://example.test/oats', 'en_GB')
        client.http = RoutedHttp(listed, details)
        return client.lookup(VIN, {**VEHICLE, 'model_year': 2013}), client.http

    def test_variants_are_grouped_by_the_grades_they_take(self):
        listed = [equipment('eng1'), equipment('eng2', displacement='2.4')]
        report, _ = self.lookup(listed, {'eng1': record('0W-20'), 'eng2': record('0W-20')})
        self.assertEqual((report['status'], report['items']), ('CONFLICTED', []))
        self.assertEqual(report['grade_groups'], [{
            'grades': ['0W-20'], 'engines': ['Example Car 2.0', 'Example Car 2.4'],
            'capacities': ['4.2'], 'fuels': []}])
        fuels = [{**equipment('eng1'), 'display_name_long': 'Example Car 2.0 (P)'},
                 {**equipment('eng2'), 'display_name_long': 'Example Car 2.0 d (D)'}]
        mixed, _ = self.lookup(fuels, {'eng1': record('0W-30'), 'eng2': record('0W-30')})
        self.assertEqual(mixed['grade_groups'][0]['fuels'], ['diesel', 'petrol'])
        report, _ = self.lookup(listed, {'eng1': record('0W-20'), 'eng2': record('5W-30')})
        self.assertEqual([group['grades'] for group in report['grade_groups']],
                         [['0W-20'], ['5W-30']])

    def test_a_variant_that_cannot_be_read_leaves_no_groups(self):
        listed = [equipment('eng1'), equipment('eng2', displacement='2.4')]
        report, _ = self.lookup(
            listed, {'eng1': record('0W-20'), 'eng2': PublicSourceError('TIME_LIMIT')})
        self.assertEqual(report['status'], 'CONFLICTED')
        self.assertNotIn('grade_groups', report)
        self.assertTrue(report['partial'])
        # One without an oil record proves nothing about the grade, and is no passing failure.
        report, _ = self.lookup(listed, {'eng1': record('0W-20'), 'eng2': {'application': []}})
        self.assertNotIn('grade_groups', report)
        self.assertNotIn('partial', report)

    def test_a_stated_hybrid_keeps_the_hybrid_variants(self):
        listed = [{**equipment('eng1'), 'display_name_long': 'Example Car 2.0 Hybrid (P)'},
                  {**equipment('eng2'), 'display_name_long': 'Example Car 2.0 (P)'}]
        client = OatsCatalog('Shell', 'https://example.test/oats', 'en_GB')
        client.http = RoutedHttp(listed, {'eng1': record('0W-16'), 'eng2': record('0W-20')})
        report = client.lookup(VIN, {**VEHICLE, 'model_year': 2013, 'fuel': 'hybrid'})
        self.assertEqual((report['status'], report['items'][0]['viscosity']),
                         ('PROVISIONAL', '0W-16'))
        # No variant says hybrid: nothing is dropped on that word.
        listed[0]['display_name_long'] = 'Example Car 2.0 e (P)'
        report, _ = self.lookup(listed, {'eng1': record('0W-16'), 'eng2': record('0W-20')})
        self.assertEqual(report['status'], 'CONFLICTED')

    def test_a_stated_engine_code_in_brackets_keeps_its_variants(self):
        listed = [{**equipment('eng1'), 'display_name_long': 'Example Car 2.0 (2AR-FE) (P)'},
                  {**equipment('eng2'), 'display_name_long': 'Example Car 2.0 (P)'}]
        client = OatsCatalog('Shell', 'https://example.test/oats', 'en_GB')
        client.http = RoutedHttp(listed, {'eng1': record('0W-20'), 'eng2': record('0W-16')})
        report = client.lookup(VIN, {**VEHICLE, 'model_year': 2013, 'engine_code': '2ar-fe'})
        self.assertEqual((report['status'], report['items'][0]['viscosity']),
                         ('PROVISIONAL', '0W-20'))
        report, _ = self.lookup(listed, {'eng1': record('0W-20'), 'eng2': record('0W-16')})
        self.assertEqual(report['status'], 'CONFLICTED')

    def test_too_many_variants_are_not_read(self):
        listed = [equipment(f'eng{n}', displacement=f'{n}.0') for n in range(10, 27)]
        report, http = self.lookup(listed, {})
        self.assertEqual((report['status'], len(http.calls)), ('CONFLICTED', 1))
        self.assertNotIn('grade_groups', report)


class OatsTests(unittest.TestCase):
    def test_one_engine_returns_oil_and_never_sends_the_vin(self):
        client = ShellOilClient()
        detail = {'equipment': {'display_name_long': 'Example Car 2.0', 'application': [{
            'name': 'Engine', 'display_capacity': '4.2',
            'product': [{'name': 'Helix Ultra 5W-30', 'external_id': 'OIL1'}],
        }]}}
        client.http = FakeHttp([
            (200, {'equipment_list': {'equipment': [equipment()]}}),
            (200, detail),
        ])
        result = client.lookup(VIN, {**VEHICLE, 'model_year': 2013, 'displacement_l': '2.0'})
        self.assertEqual(result['status'], 'PROVISIONAL')
        self.assertEqual(result['items'][0]['product_code'], 'OIL1')
        self.assertEqual(result['items'][0]['viscosity'], '5W-30')
        self.assertEqual(result['items'][0]['capacity'], '4.2')
        self.assertNotIn(VIN, json.dumps(client.http.calls))
        self.assertNotIn(VIN, json.dumps(result))

    def test_two_engines_return_no_product(self):
        client = OatsCatalog('Shell', 'https://example.test/oats', 'en_GB')
        client.http = RoutedHttp(
            [equipment('eng1'), equipment('eng2', displacement='2.4')],
            {'eng1': record('0W-20'), 'eng2': record('5W-30')})
        result = client.lookup(VIN, {**VEHICLE, 'model_year': 2013})
        self.assertEqual(result['status'], 'CONFLICTED')
        self.assertEqual(result['items'], [])
        self.assertIn('რამდენიმე ძრავა', result['clarification'])


class FuchsTests(unittest.TestCase):
    def test_single_type_reads_engine_oil_only(self):
        client = FuchsClient()
        client.http = FakeHttp([
            (200, {'resultData': [{'id': 1, 'categoryName': 'Cars'}]}),
            (200, {'resultData': [{'id': 9, 'makeName': 'Example'}]}),
            (200, {'resultData': [{'id': 4, 'modelName': 'Car VII'}]}),
            (200, {'resultData': [{'id': 7, 'typeName': 'Car 2.0', 'yearStart': 2012,
                                   'yearEnd': 2014}]}),
            (200, {'resultData': {
                'makeName': 'Example', 'modelName': 'Car', 'typeName': 'Car 2.0',
                'components': [
                    {'componentName': 'Cooling system', 'productRecommendations': [
                        {'productName': 'Coolant', 'productCode': 'NO'}]},
                    {'componentName': 'Engine', 'productRecommendations': [
                        {'productName': 'TITAN 5W-30', 'productCode': 'CP1', 'intervals': [
                            {'intervalName': '12 months'}]}]},
                ],
            }}),
        ])
        result = client.lookup(VIN, VEHICLE)
        self.assertEqual(result['items'][0]['product_code'], 'CP1')
        self.assertEqual(result['items'][0]['interval'], '12 months')
        self.assertNotIn('Coolant', json.dumps(result))

    def test_trim_selects_one_type_from_several(self):
        client = FuchsClient()
        client.http = FakeHttp([
            (200, {'resultData': [{'id': 1, 'categoryName': 'Cars'}]}),
            (200, {'resultData': [{'id': 9, 'makeName': 'Example'}]}),
            (200, {'resultData': [{'id': 4, 'modelName': 'Car'}]}),
            (200, {'resultData': [
                {'id': 7, 'typeName': 'Car E 350', 'yearStart': 2012, 'yearEnd': 2014},
                {'id': 8, 'typeName': 'Car E 400', 'yearStart': 2012, 'yearEnd': 2014},
            ]}),
            (200, {'resultData': {'makeName': 'Example', 'modelName': 'Car',
                                  'typeName': 'Car E 400', 'components': [
                {'componentName': 'Engine', 'productRecommendations': [
                    {'productName': 'TITAN 5W-40', 'productCode': 'CP9'}]}]}}),
        ])
        result = client.lookup(VIN, {**VEHICLE, 'trim': 'E400'})
        self.assertEqual(result['items'][0]['product_code'], 'CP9')
        self.assertIn('/recommendations/8', client.http.calls[-1][1])

    def test_only_that_years_generations_are_opened_and_the_trim_narrows_a_series(self):
        client = FuchsClient()
        series = [{'id': 20 + n, 'typeName': f'3{n:02d}i', 'yearStart': 2012, 'yearEnd': 2015}
                  for n in range(12)]
        client.http = FakeHttp([
            (200, {'resultData': [{'id': 1, 'categoryName': 'Cars'}]}),
            (200, {'resultData': [{'id': 9, 'makeName': 'Example'}]}),
            (200, {'resultData': [
                {'id': 3, 'modelName': '3 series', 'typeYearStart': 2005, 'typeYearEnd': 2011},
                {'id': 4, 'modelName': '3 series', 'typeYearStart': 2012, 'typeYearEnd': 2019},
                {'id': 5, 'modelName': 'X3', 'typeYearStart': 2012, 'typeYearEnd': 2017},
            ]}),
            (200, {'resultData': series + [
                {'id': 8, 'typeName': '328i', 'yearStart': 2012, 'yearEnd': 2015}]}),
            (200, {'resultData': {'makeName': 'Example', 'modelName': '3 series',
                                  'typeName': '328i', 'components': [
                {'componentName': 'Engine', 'productRecommendations': [
                    {'productName': 'TITAN 0W-20', 'productCode': 'CP3'}]}]}}),
        ])
        result = client.lookup(VIN, {**VEHICLE, 'model': '3 Series', 'model_year': 2014,
                                     'trim': '328i'})
        self.assertEqual(result['items'][0]['product_code'], 'CP3')
        self.assertIn('yearFilter=2014', client.http.calls[2][1])
        self.assertIn('modelId=4', client.http.calls[3][1])  # Not the 2005-2011 generation.
        self.assertEqual(len(client.http.calls), 5)

    def test_two_types_do_not_request_a_recommendation(self):
        client = FuchsClient()
        client.http = FakeHttp([
            (200, {'resultData': [{'id': 1, 'categoryName': 'Cars'}]}),
            (200, {'resultData': [{'id': 9, 'makeName': 'Example'}]}),
            (200, {'resultData': [{'id': 4, 'modelName': 'Car'}]}),
            (200, {'resultData': [
                {'id': 7, 'typeName': 'Car 2.0', 'yearStart': 2012, 'yearEnd': 2014},
                {'id': 8, 'typeName': 'Car 2.4', 'yearStart': 2012, 'yearEnd': 2014},
            ]}),
        ])
        result = client.lookup(VIN, VEHICLE)
        self.assertEqual(result['status'], 'CONFLICTED')
        self.assertEqual(result['items'], [])
        self.assertEqual(len(client.http.calls), 4)


class JsFilterTests(unittest.TestCase):
    def test_one_table_row_keeps_filter_codes_and_redacts_a_vin(self):
        client = JsFilterClient()
        client.http = FakeHttp([(200, ONE_ROW.replace('1999-2003', VIN).encode())])
        result = client.lookup(VIN, VEHICLE)
        self.assertEqual(result['status'], 'PROVISIONAL')
        self.assertEqual([item['product_code'] for item in result['items']], ['C307J', 'A867J'])
        self.assertNotIn(VIN, json.dumps(result))
        self.assertEqual(redact(VIN), '[VIN]')

    def test_two_rows_withhold_codes(self):
        client = JsFilterClient()
        client.http = FakeHttp([(200, TWO_ROWS.encode())])
        result = client.lookup(VIN, VEHICLE)
        self.assertEqual(result['status'], 'CONFLICTED')
        self.assertEqual(result['items'], [])
        self.assertNotIn('C307J', json.dumps(result))

    def test_recognition_without_a_table_is_empty(self):
        client = JsFilterClient()
        client.http = FakeHttp([(200, b'<h2>VIN Number found.</h2>')])
        result = client.lookup(VIN, VEHICLE)
        self.assertEqual(result['status'], 'EMPTY')
        self.assertEqual(result['items'], [])


class FluidFederationTests(FederationTests):
    def test_fluid_report_is_separate_from_warehouse_parts(self):
        fuchs = Mock()
        fuchs.lookup.return_value = {
            'provider': 'Fuchs', 'status': 'PROVISIONAL', 'vehicle_label': 'Example Car',
            'items': [{'kind': 'oil', 'component': 'Engine', 'product_name': 'TITAN 5W-30',
                       'product_code': 'CP1', 'viscosity': '5W-30', 'interval': None,
                       'capacity': None}],
            'clarification': None, 'warnings': ['provisional'],
        }
        coordinator = self.create_coordinator(fuchs_service=fuchs, fuchs_enabled=True)
        result = self.finish(coordinator)
        self.assertEqual(self.source(result, 'fuchs')['status'], 'OK')
        self.assertEqual(result['fluids'][0]['items'][0]['product_code'], 'CP1')
        self.assertNotIn('CP1', json.dumps(result['items']))
        self.assertEqual(self.source(result, 'shell')['status'], 'DISABLED')
        self.assertNotIn(VIN, json.dumps(fuchs.lookup.call_args.args[1]))
