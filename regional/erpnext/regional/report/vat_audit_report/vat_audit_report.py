# Copyright (c) 2013, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

from __future__ import unicode_literals
import frappe
import json
from frappe import _
from frappe.utils import formatdate

def execute(filters=None):
	return VATAuditReport(filters).run()

class VATAuditReport(object):

	def __init__(self, filters=None):
		self.filters = frappe._dict(filters or {})
		self.columns = []
		self.data = []
		self.doctypes = ["Purchase Invoice", "Sales Invoice"]

	def run(self):
		self.get_sa_vat_accounts()
		self.get_columns()
		for doctype in self.doctypes:
			self.select_columns = """
			name as voucher_no,
			posting_date, remarks"""
			columns = ", supplier as party, credit_to as account" if doctype=="Purchase Invoice" \
				else ", customer as party, debit_to as account"
			self.select_columns += columns

			self.get_invoice_data(doctype)

			if self.invoices:
				self.get_invoice_items(doctype)
				self.get_items_based_on_tax_rate(doctype)
				self.get_data(doctype)

		return self.columns, self.data

	def get_sa_vat_accounts(self):
		self.sa_vat_accounts = frappe.get_list("South Africa VAT Account",
			filters = {"parent": self.filters.company}, pluck="account")
		if not self.sa_vat_accounts and not frappe.flags.in_test and not frappe.flags.in_migrate:
			frappe.throw(_("Please set VAT Accounts in South Africa VAT Settings"))

	def get_invoice_data(self, doctype):
		conditions = self.get_conditions()
		self.invoices = frappe._dict()

		invoice_data = frappe.db.sql("""
			SELECT
				{select_columns}
			FROM
				`tab{doctype}`
			WHERE
				docstatus = 1 {where_conditions}
				and is_opening = 'No'
			ORDER BY
				posting_date DESC
			""".format(select_columns=self.select_columns, doctype=doctype,
				where_conditions=conditions), self.filters, as_dict=1)

		for d in invoice_data:
			self.invoices.setdefault(d.voucher_no, d)

	def get_invoice_items(self, doctype):
		self.invoice_items = frappe._dict()
		self.item_tax_rate = frappe._dict()

		items = frappe.db.sql("""
			SELECT
				item_code, parent, taxable_value, base_net_amount, item_tax_rate
			FROM
				`tab%s Item`
			WHERE
				parent in (%s)
			""" % (doctype, ', '.join(['%s']*len(self.invoices))), tuple(self.invoices), as_dict=1)
		for d in items:
			if d.item_code not in self.invoice_items.get(d.parent, {}):
				self.invoice_items.setdefault(d.parent, {}).setdefault(d.item_code,
				sum((i.get('taxable_value', 0) or i.get('base_net_amount', 0)) for i in items
				if i.item_code == d.item_code and i.parent == d.parent))

	def get_items_based_on_tax_rate(self, doctype):
		self.items_based_on_tax_rate = frappe._dict()
		self.tax_doctype = "Purchase Taxes and Charges" if doctype=="Purchase Invoice" \
			else "Sales Taxes and Charges"
		self.tax_details = frappe.db.sql("""
			SELECT
				parent, account_head, item_wise_tax_detail, base_tax_amount_after_discount_amount
			FROM
				`tab%s`
			WHERE
				parenttype = %s and docstatus = 1
				and parent in (%s)
			ORDER BY
				account_head
			""" % (self.tax_doctype, '%s', ', '.join(['%s']*len(self.invoices.keys()))),
			tuple([doctype] + list(self.invoices.keys())))

		for parent, account, item_wise_tax_detail, tax_amount in self.tax_details:
			if item_wise_tax_detail:
				try:
					if account in self.sa_vat_accounts:
						item_wise_tax_detail = json.loads(item_wise_tax_detail)
					else:
						continue
					for item_code, taxes in item_wise_tax_detail.items():
						is_zero_rated = frappe.get_value("Item", item_code, "is_zero_rated")
						if taxes[0] == 0 and not is_zero_rated:
							continue
						tax_rate, item_amount_map = self.get_item_amount_map(parent, item_code, taxes)

						if tax_rate is not None:
							rate_based_dict = self.items_based_on_tax_rate.setdefault(parent, {}) \
								.setdefault(tax_rate, [])
							if item_code not in rate_based_dict:
								rate_based_dict.append(item_code)
				except ValueError:
					continue

	def get_item_amount_map(self, parent, item_code, taxes):
		net_amount = abs(self.invoice_items.get(parent).get(item_code))
		tax_rate = taxes[0]
		tax_amount = taxes[1]
		gross_amount = net_amount + tax_amount
		item_amount_map = self.item_tax_rate.setdefault(parent, {}) \
			.setdefault(item_code, [])
		amount_dict = {
			"tax_rate": tax_rate,
			"gross_amount": gross_amount,
			"tax_amount": tax_amount,
			"net_amount": net_amount
		}
		item_amount_map.append(amount_dict)

		return tax_rate, item_amount_map

	def get_conditions(self):
		conditions = ""
		for opts in (("company", " and company=%(company)s"),
			("from_date", " and posting_date>=%(from_date)s"),
			("to_date", " and posting_date<=%(to_date)s")):
			if self.filters.get(opts[0]):
				conditions += opts[1]

		return conditions

	def get_data(self, doctype):
		consolidated_data = self.get_consolidated_data(doctype)
		section_name = _("Purchases") if doctype == "Purchase Invoice" else _("Sales")

		for rate, section in consolidated_data.items():
			rate = int(rate)
			label = frappe.bold(section_name + "- " + "Rate" + " " + str(rate) + "%")
			section_head = {"posting_date": label}
			total_gross = total_tax = total_net = 0
			self.data.append(section_head)
			for row in section.get("data"):
				self.data.append(row)
				total_gross += row["gross_amount"]
				total_tax += row["tax_amount"]
				total_net += row["net_amount"]

			total = {
				"posting_date": frappe.bold(_("Total")),
				"gross_amount": total_gross,
				"tax_amount": total_tax,
				"net_amount": total_net,
				"bold":1
			}
			self.data.append(total)
			self.data.append({})

	def get_consolidated_data(self, doctype):
		consolidated_data_map={}
		for inv, inv_data in self.invoices.items():
			if self.items_based_on_tax_rate.get(inv):
				for rate, items in self.items_based_on_tax_rate.get(inv).items():
					consolidated_data_map.setdefault(rate, {"data": []})
					for item in items:
						row = {}
						item_details = self.item_tax_rate.get(inv).get(item)
						row["account"] = inv_data.get("account")
						row["posting_date"] = formatdate(inv_data.get("posting_date"), 'dd-mm-yyyy')
						row["voucher_type"] = doctype
						row["voucher_no"] = inv
						row["remarks"] = inv_data.get("remarks")
						row["gross_amount"]= item_details[0].get("gross_amount")
						row["tax_amount"]= item_details[0].get("tax_amount")
						row["net_amount"]= item_details[0].get("net_amount")
						consolidated_data_map[rate]["data"].append(row)

		return consolidated_data_map

	def get_columns(self):
		self.columns = [
			{
				"fieldname": "posting_date",
				"label": "Posting Date",
				"fieldtype": "Data",
				"width": 200
			},
			{
				"fieldname": "account",
				"label": "Account",
				"fieldtype": "Link",
				"options": "Account",
				"width": 150
			},
			{
				"fieldname": "voucher_type",
				"label": "Voucher Type",
				"fieldtype": "Data",
				"width": 140,
				"hidden": 1
			},
			{
				"fieldname": "voucher_no",
				"label": "Reference",
				"fieldtype": "Dynamic Link",
				"options": "voucher_type",
				"width": 150
			},
			{
				"fieldname": "remarks",
				"label": "Details",
				"fieldtype": "Data",
				"width": 150
			},
			{
				"fieldname": "net_amount",
				"label": "Net Amount",
				"fieldtype": "Currency",
				"width": 150
			},
			{
				"fieldname": "tax_amount",
				"label": "Tax Amount",
				"fieldtype": "Currency",
				"width": 150
			},
			{
				"fieldname": "gross_amount",
				"label": "Gross Amount",
				"fieldtype": "Currency",
				"width": 150
			},
		]
