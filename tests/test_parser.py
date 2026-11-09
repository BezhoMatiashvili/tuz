import unittest

from warehouse_sync.parser import parse_products_page


class ParseProductsPageTests(unittest.TestCase):
    def test_parser_preserves_all_source_columns_and_known_fields(self) -> None:
        html = """
        <tr class="loader product_names" id="65805">
          <td id="product_code65805">2040009881</td>
          <td class="product_names">სამუხრუჭე <strong>დისკი</strong> უკ.</td>
          <td>VAG PASSAT</td><td>DF4754</td><td>DF4754/RN1739</td>
          <td>240</td><td>0</td><td>TRW</td><td></td><td>0</td><td>0</td>
        </tr>
        <button onclick="LoadMoreData(1)">1</button>
        <button onclick="LoadMoreData(1317)">1317</button>
        """

        parsed = parse_products_page(html)

        self.assertEqual(parsed.total_pages, 1317)
        self.assertEqual(len(parsed.products), 1)
        product = parsed.products[0]
        self.assertEqual(product.source_product_id, "65805")
        self.assertEqual(product.product_code, "2040009881")
        self.assertEqual(product.product_name, "სამუხრუჭე დისკი უკ.")
        self.assertEqual(product.product_model, "VAG PASSAT")
        self.assertEqual(product.original_code, "DF4754")
        self.assertEqual(product.duplicate_code, "DF4754/RN1739")
        self.assertEqual(product.brand, "TRW")
        self.assertEqual(product.source_columns["column_10"], "0")
        self.assertEqual(len(product.row_hash), 64)


    def test_parser_hash_changes_when_any_source_column_changes(self) -> None:
        before = '<tr class="product_names" id="1"><td>A</td><td>Product</td><td>0</td></tr>'
        after = '<tr class="product_names" id="1"><td>A</td><td>Product</td><td>1</td></tr>'

        before_product = parse_products_page(before).products[0]
        after_product = parse_products_page(after).products[0]

        self.assertNotEqual(before_product.row_hash, after_product.row_hash)

    def test_parser_never_preserves_malicious_source_html(self) -> None:
        html = (
            '<tr class="product_names" id="1"><td>A</td>'
            '<td><script>alert(1)</script><img src=x onerror=alert(2)>Product</td></tr>'
        )

        product = parse_products_page(html).products[0]

        self.assertEqual(product.source_columns["column_02"], "alert(1)Product")
        self.assertNotIn("<script", " ".join(product.source_columns.values()).lower())
        self.assertFalse(hasattr(product, "source_row_html"))
