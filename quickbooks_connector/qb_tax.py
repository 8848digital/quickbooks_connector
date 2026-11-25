"""Tax handling for QuickBooks payloads, per QuickBooks region.

UK (VAT): every line carries a VAT TaxCodeRef picked from the invoice's tax
rate, and the transaction sets GlobalTaxCalculation. This is the original
behaviour of the app and must not change.

US (sales tax), per Intuit "Manage sales tax for US locales":
- sales lines carry TaxCodeRef "TAX" or "NON";
- the rate is chosen at transaction level with TxnTaxDetail.TxnTaxCodeRef;
- companies on Automated Sales Tax (AST) calculate tax themselves, and the
  ERPNext amount is forced with TxnTaxDetail.TotalTax;
- GlobalTaxCalculation is not supported and is never sent;
- bills and vendor credits take no line tax codes.

TODO: non-tax charges in ERPNext's taxes table (freight, handling, etc.)
are not pushed to QuickBooks in either region.
"""

import frappe
from frappe.utils import flt

REGION_UK = "UK"
REGION_US = "US"

US_TAXABLE = "TAX"
US_NON_TAXABLE = "NON"


def get_region(settings):
	"""Return "US" or "UK". Anything other than "US" (including blank) is UK."""
	region = (getattr(settings, "qb_region", None) or REGION_UK).strip().upper()
	return REGION_US if region == REGION_US else REGION_UK


def is_us(settings):
	return get_region(settings) == REGION_US
