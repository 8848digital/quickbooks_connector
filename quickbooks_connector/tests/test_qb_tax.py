"""Unit tests for qb_tax.py and the US payloads built with it.

QuickBooks is never called: see tests/utils.py.
"""

import json
import os
import unittest
from types import SimpleNamespace
from unittest import mock

import frappe

from quickbooks_connector import qb_tax
from quickbooks_connector.tests.utils import (
	FREIGHT_ACCOUNT,
	fake_get_value,
	make_pi,
	make_return,
	make_settings,
	make_si,
	run_builder,
	tax_row,
)


def us_settings(**overrides):
	values = dict(qb_region="US", us_sales_tax_code="5", us_uses_ast=0)
	values.update(overrides)
	return make_settings(**values)


class DbMocked(unittest.TestCase):
	"""Mock frappe.db so Account.account_type lookups never hit the database."""

	def setUp(self):
		patcher = mock.patch.object(frappe, "db", mock.MagicMock())
		patcher.start()
		self.addCleanup(patcher.stop)
		frappe.db.get_value.side_effect = fake_get_value


class TestRegion(unittest.TestCase):
	def test_default_is_uk(self):
		self.assertEqual(qb_tax.get_region(make_settings()), "UK")
		self.assertEqual(qb_tax.get_region(make_settings(qb_region="")), "UK")
		self.assertEqual(qb_tax.get_region(make_settings(qb_region=None)), "UK")

	def test_us(self):
		self.assertEqual(qb_tax.get_region(make_settings(qb_region="US")), "US")
		self.assertEqual(qb_tax.get_region(make_settings(qb_region=" us ")), "US")


class TestUKMapping(unittest.TestCase):
	def test_rates(self):
		s = make_settings()
		self.assertEqual(qb_tax.uk_tax_code(20, s), "3")
		self.assertEqual(qb_tax.uk_tax_code(5, s), "8")
		self.assertEqual(qb_tax.uk_tax_code(1, s), "10")
		self.assertEqual(qb_tax.uk_tax_code(0, s), "12")

	def test_boundaries(self):
		s = make_settings()
		self.assertEqual(qb_tax.uk_tax_code(25, s), "3")
		self.assertEqual(qb_tax.uk_tax_code(19.99, s), "8")
		self.assertEqual(qb_tax.uk_tax_code(4.99, s), "10")
		self.assertEqual(qb_tax.uk_tax_code(None, s), "12")

	def test_default_tax_code_setting(self):
		self.assertEqual(qb_tax.uk_tax_code(0, make_settings(default_tax_code="99")), "99")
		self.assertEqual(qb_tax.uk_tax_code(0, make_settings(default_tax_code="")), "12")
		self.assertEqual(qb_tax.uk_tax_code(0, SimpleNamespace()), "12")

	def test_invoice_tax_rate_is_first_positive_row(self):
		doc = make_si(0, extra_taxes=[tax_row(0, 3), tax_row(5, 5), tax_row(20, 20)])
		self.assertEqual(qb_tax.invoice_tax_rate(doc), 5)
		self.assertEqual(qb_tax.invoice_tax_rate(make_si(0)), 0)

	def test_uk_apply_sales_tax(self):
		s = make_settings()
		self.assertEqual(qb_tax.apply_sales_tax({}, make_si(20), s), {"GlobalTaxCalculation": "TaxExcluded"})
		self.assertEqual(qb_tax.apply_sales_tax({}, make_si(0), s), {"GlobalTaxCalculation": "NotApplicable"})

	def test_uk_expense_line_tax_code(self):
		s = make_settings()
		self.assertEqual(qb_tax.expense_line_tax_code(make_pi(20), s), "3")
		self.assertEqual(qb_tax.expense_line_tax_code(make_pi(20), s, uk_fixed="12"), "12")


class TestUSHelpers(DbMocked):
	def test_sales_line_tax_code(self):
		s = us_settings()
		self.assertEqual(qb_tax.sales_line_tax_code(make_si(8.25), s), "TAX")
		self.assertEqual(qb_tax.sales_line_tax_code(make_si(0), s), "NON")

	def test_taxed_ignores_rate_uses_account_type(self):
		s = us_settings()
		actual_tax = tax_row(0, 7.0, charge_type="Actual")  # rate 0, Tax account
		self.assertEqual(qb_tax.sales_line_tax_code(make_si(0, extra_taxes=[actual_tax]), s), "TAX")
		freight = tax_row(10, 10.0, account=FREIGHT_ACCOUNT)  # rate > 0, not a Tax account
		self.assertEqual(qb_tax.sales_line_tax_code(make_si(0, extra_taxes=[freight]), s), "NON")
		zero = tax_row(8, 0)  # Tax account but no amount
		self.assertEqual(qb_tax.sales_line_tax_code(make_si(0, extra_taxes=[zero]), s), "NON")

	def test_total_tax_excludes_freight_and_uses_after_discount(self):
		doc = make_si(
			0,
			extra_taxes=[
				tax_row(8, 8.0, after_discount=7.5),
				tax_row(2, 2.0),
				tax_row(0, 15.0, account=FREIGHT_ACCOUNT),
			],
		)
		self.assertEqual(qb_tax.us_total_tax(doc), 9.5)

	def test_total_tax_positive_for_returns(self):
		self.assertEqual(qb_tax.us_total_tax(make_return(make_si(20))), 20.0)

	def test_apply_sales_tax_ast_off(self):
		payload = qb_tax.apply_sales_tax({"GlobalTaxCalculation": "x"}, make_si(20), us_settings())
		self.assertEqual(payload, {"TxnTaxDetail": {"TxnTaxCodeRef": {"value": "5"}}})

	def test_apply_sales_tax_ast_on(self):
		payload = qb_tax.apply_sales_tax({}, make_si(20), us_settings(us_uses_ast=1))
		self.assertEqual(payload, {"TxnTaxDetail": {"TotalTax": 20.0}})

	def test_apply_sales_tax_untaxed(self):
		self.assertEqual(qb_tax.apply_sales_tax({}, make_si(0), us_settings()), {})
		self.assertEqual(qb_tax.apply_sales_tax({}, make_si(0), us_settings(us_uses_ast=1)), {})

	def test_apply_sales_tax_requires_code_when_ast_off(self):
		with self.assertRaises(frappe.ValidationError):
			qb_tax.apply_sales_tax({}, make_si(20), us_settings(us_sales_tax_code=""))

	def test_expense_line_tax_code_is_none(self):
		self.assertIsNone(qb_tax.expense_line_tax_code(make_pi(20), us_settings()))
		self.assertIsNone(qb_tax.expense_line_tax_code(make_pi(20), us_settings(), uk_fixed="12"))

	def test_apply_purchase_tax(self):
		payload = {"Line": [], "GlobalTaxCalculation": "x"}
		qb_tax.apply_purchase_tax(payload, make_pi(20), us_settings(), "77", uk_fixed_global="NotApplicable")
		self.assertNotIn("GlobalTaxCalculation", payload)
		self.assertEqual(
			payload["Line"],
			[
				{
					"DetailType": "AccountBasedExpenseLineDetail",
					"Amount": 20.0,
					"Description": "Sales tax",
					"AccountBasedExpenseLineDetail": {"AccountRef": {"value": "77"}},
				}
			],
		)
		untaxed = {"Line": []}
		qb_tax.apply_purchase_tax(untaxed, make_pi(0), us_settings(), "77")
		self.assertEqual(untaxed, {"Line": []})


def us_sales_line(tax_code, amount=100.0, qty=2.0):
	return {
		"DetailType": "SalesItemLineDetail",
		"Amount": amount,
		"Description": "ITEM-1 description",
		"SalesItemLineDetail": {
			"ItemRef": {"value": "11"},
			"Qty": qty,
			"UnitPrice": 50.0,
			"TaxCodeRef": {"value": tax_code},
		},
	}


US_TAX_LINE = {
	"DetailType": "AccountBasedExpenseLineDetail",
	"Amount": 20.0,
	"Description": "Sales tax",
	"AccountBasedExpenseLineDetail": {"AccountRef": {"value": "77"}},
}


class TestUSPayloads(unittest.TestCase):
	"""Full payloads from the six builders with qb_region = US."""

	def assertNoGlobalTax(self, payload):
		self.assertNotIn("GlobalTaxCalculation", json.dumps(payload))

	def test_invoice_taxed_ast_off(self):
		payload = run_builder("invoice", make_si(20), us_settings())
		self.assertNoGlobalTax(payload)
		self.assertEqual(payload["Line"], [us_sales_line("TAX")])
		self.assertEqual(payload["TxnTaxDetail"], {"TxnTaxCodeRef": {"value": "5"}})

	def test_invoice_taxed_ast_on(self):
		payload = run_builder("invoice", make_si(20), us_settings(us_uses_ast=1))
		self.assertNoGlobalTax(payload)
		self.assertEqual(payload["Line"], [us_sales_line("TAX")])
		self.assertEqual(payload["TxnTaxDetail"], {"TotalTax": 20.0})

	def test_invoice_untaxed(self):
		for ast in (0, 1):
			payload = run_builder("invoice", make_si(0), us_settings(us_uses_ast=ast))
			self.assertNoGlobalTax(payload)
			self.assertEqual(payload["Line"], [us_sales_line("NON")])
			self.assertNotIn("TxnTaxDetail", payload)

	def test_invoice_freight_only_is_untaxed(self):
		doc = make_si(0, extra_taxes=[tax_row(0, 15.0, account=FREIGHT_ACCOUNT)])
		payload = run_builder("invoice", doc, us_settings(us_uses_ast=1))
		self.assertEqual(payload["Line"], [us_sales_line("NON")])
		self.assertNotIn("TxnTaxDetail", payload)

	def test_credit_memo_ast_on(self):
		doc = make_return(make_si(20, name="SINV-RET-0001"))
		payload = run_builder("credit_memo", doc, us_settings(us_uses_ast=1))
		self.assertNoGlobalTax(payload)
		self.assertEqual(payload["Line"], [us_sales_line("TAX")])
		self.assertEqual(payload["TxnTaxDetail"], {"TotalTax": 20.0})

	def test_amend_invoice_ast_off(self):
		doc = make_si(20, name="SINV-0001-1", amended_from="SINV-0001")
		payload = run_builder("amend_invoice", doc, us_settings())
		self.assertNoGlobalTax(payload)
		self.assertEqual(payload["Id"], "900")
		self.assertEqual(payload["TxnTaxDetail"], {"TxnTaxCodeRef": {"value": "5"}})

	def test_bill_taxed(self):
		payload = run_builder("bill", make_pi(20), us_settings())
		self.assertNoGlobalTax(payload)
		self.assertNotIn("TaxCodeRef", json.dumps(payload))
		self.assertEqual(
			payload["Line"],
			[
				{
					"DetailType": "AccountBasedExpenseLineDetail",
					"Amount": 100.0,
					"Description": "ITEM-1: ITEM-1 description",
					"AccountBasedExpenseLineDetail": {"AccountRef": {"value": "77"}},
				},
				US_TAX_LINE,
			],
		)

	def test_bill_untaxed(self):
		payload = run_builder("bill", make_pi(0), us_settings())
		self.assertNoGlobalTax(payload)
		self.assertEqual(len(payload["Line"]), 1)
		self.assertNotIn("TaxCodeRef", json.dumps(payload))

	def test_vendor_credit_taxed(self):
		doc = make_return(make_pi(20, name="PINV-RET-0001"))
		payload = run_builder("vendor_credit", doc, us_settings())
		self.assertNoGlobalTax(payload)
		self.assertNotIn("TaxCodeRef", json.dumps(payload))
		self.assertEqual(payload["Line"][-1], US_TAX_LINE)
		self.assertEqual([line["Amount"] for line in payload["Line"]], [100.0, 20.0])

	def test_amend_bill_taxed(self):
		doc = make_pi(20, name="PINV-0001-1", amended_from="PINV-0001")
		payload = run_builder("amend_bill", doc, us_settings())
		self.assertNoGlobalTax(payload)
		self.assertNotIn("TaxCodeRef", json.dumps(payload))
		self.assertEqual(payload["Line"][-1], US_TAX_LINE)


class TestPreferencesAndTaxCodes(unittest.TestCase):
	def test_fetch_us_uses_ast(self):
		from quickbooks_connector.api import fetch_us_uses_ast

		api = mock.MagicMock()
		api.make_request.return_value = {"Preferences": {"TaxPrefs": {"PartnerTaxEnabled": True}}}
		self.assertEqual(fetch_us_uses_ast(api), 1)
		self.assertEqual(api.make_request.call_args.kwargs["params"], {"minorversion": 65})
		api.make_request.return_value = {"Preferences": {"TaxPrefs": {"PartnerTaxEnabled": False}}}
		self.assertEqual(fetch_us_uses_ast(api), 0)

	def test_fetch_us_uses_ast_failure_is_logged_not_raised(self):
		from quickbooks_connector.api import fetch_us_uses_ast

		api = mock.MagicMock()
		api.make_request.side_effect = Exception("boom")
		with mock.patch.object(frappe, "log_error") as log_error:
			self.assertIsNone(fetch_us_uses_ast(api))
		log_error.assert_called_once()

	def test_get_us_tax_codes(self):
		from quickbooks_connector import api

		instance = mock.MagicMock()
		instance.make_request.return_value = {
			"QueryResponse": {
				"TaxCode": [
					{"Id": "5", "Name": "California", "Active": True, "Taxable": True, "SyncToken": "0"},
					{"Id": "NON", "Name": "NON", "Active": True, "Taxable": False},
				]
			}
		}
		with mock.patch.object(api, "QuickBooksAPI", return_value=instance), mock.patch.object(
			frappe, "only_for"
		) as only_for:
			codes = api.get_us_tax_codes()
		only_for.assert_called_once_with("System Manager")
		self.assertIn("TaxCode", instance.make_request.call_args.args[0])
		self.assertEqual(
			codes,
			[
				{"Id": "5", "Name": "California", "Active": True, "Taxable": True},
				{"Id": "NON", "Name": "NON", "Active": True, "Taxable": False},
			],
		)


class TestSettingDefaults(unittest.TestCase):
	"""New fields must default to what getattr() fell back to before they existed."""

	def test_defaults_match_old_fallbacks(self):
		path = os.path.join(
			os.path.dirname(qb_tax.__file__),
			"quickbooks_connector",
			"doctype",
			"quickbooks_setting",
			"quickbooks_setting.json",
		)
		with open(path) as f:
			fields = {df["fieldname"]: df for df in json.load(f)["fields"]}
		expected = {
			"qb_region": "UK",
			"us_uses_ast": "0",
			"sync_accounts": "1",
			"sync_payments": "1",
			"sync_bill_payments": "1",
			"sync_customers": "0",
			"sync_items": "0",
			"sync_suppliers": "0",
			"enable_auto_sync": "0",
			"auto_sync_sales_invoice": "0",
			"auto_sync_purchase_invoice": "0",
		}
		for fieldname, default in expected.items():
			self.assertEqual(fields[fieldname].get("default"), default, fieldname)
		self.assertEqual(fields["us_uses_ast"].get("read_only"), 1)
		self.assertEqual(fields["qb_region"]["options"], "UK\nUS")
