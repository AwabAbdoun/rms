# -*- coding: utf-8 -*-
# Copyright (c) 2018, Awab Abdoun and Mohammed Elamged and contributors
# For license information, please see license.txt

from __future__ import unicode_literals
import frappe
from frappe.model.document import Document

from frappe.utils import cstr, flt, getdate, new_line_sep, nowdate, add_days
from frappe import msgprint, _
from frappe.model.mapper import get_mapped_doc
from rms.stock.stock_balance import update_bin_qty, get_indented_qty
from rms.manufacturing.doctype.production_order.production_order import get_item_details
from rms.controllers.stock_controller import StockController

from rms.stock.doctype.item.item import validate_end_of_life

form_grid_templates = {
	"items": "templates/form_grid/material_request_grid.html"
}

class MaterialRequest(StockController):
	def get_feed(self):
		return _("{0}: {1}").format(self.status, self.material_request_type)

	def check_if_already_pulled(self):
		pass

	# Validate
	# ---------------------
	def validate(self):
		# super(MaterialRequest, self).validate()

		self.validate_schedule_date()

		if not self.status:
			self.status = "Draft"

		from rms.controllers.status_updater import validate_status
		validate_status(self.status,
			["Draft", "Submitted", "Stopped", "Cancelled", "Pending",
			"Partially Ordered", "Ordered", "Issued", "Transferred"])

		validate_for_items(self)

		# self.set_title()
		# self.validate_qty_against_so()
		# NOTE: Since Item BOM and FG quantities are combined, using current data, it cannot be validated
		# Though the creation of Material Request from a Production Plan can be rethought to fix this

	def validate_for_items(self):
		items = []
		for d in self.get("items"):
			if not d.qty:
				frappe.throw(_("Please enter quantity for Item {0}").format(d.item_code))

			# update with latest quantities
			bin = frappe.db.sql("""select projected_qty from `tabBin` where
				item_code = %s and warehouse = %s""", (d.item_code, d.warehouse), as_dict=1)

			f_lst ={'projected_qty': bin and flt(bin[0]['projected_qty']) or 0, 'ordered_qty': 0, 'received_qty' : 0}
			for x in f_lst :
				if d.meta.get_field(x):
					d.set(x, f_lst[x])

			item = frappe.db.sql("""select is_stock_item,
				end_of_life, disabled from `tabItem` where name=%s""",
				d.item_code, as_dict=1)[0]

			validate_end_of_life(d.item_code, item.end_of_life, item.disabled)

			# validate stock item
			if item.is_stock_item==1 and d.qty and not d.warehouse:
				frappe.throw(_("Warehouse is mandatory for stock Item {0} in row {1}").format(d.item_code, d.idx))

			items.append(cstr(d.item_code))

		if items and len(items) != len(set(items)):
			frappe.throw(_("Same item cannot be entered multiple times."))

	def validate_schedule_date(self):
		if not self.schedule_date:
			self.schedule_date = min([d.schedule_date for d in self.get("items")])

		if self.schedule_date:
			for d in self.get('items'):
				if not d.schedule_date:
					d.schedule_date = self.schedule_date

				if d.schedule_date and getdate(d.schedule_date) < getdate(self.transaction_date):
					frappe.throw(_("Expected Date cannot be before Transaction Date"))
		else:
			frappe.throw(_("Please enter Schedule Date"))

	def set_title(self):
		'''Set title as comma separated list of items'''
		items = []
		for d in self.items:
			if d.item_code not in items:
				items.append(d.item_code)
			if(len(items)==4):
				break

		self.title = ', '.join(items)

	def on_submit(self):
		# frappe.db.set(self, 'status', 'Submitted')
		self.update_requested_qty()

	def before_save(self):
		self.set_status(update=True)

	def before_submit(self):
		self.set_status(update=True)

	def before_cancel(self):
		# if MRQ is already closed, no point saving the document
		check_for_closed_status(self.doctype, self.name)
		self.set_status(update=True, status='Cancelled')

	def check_modified_date(self):
		mod_db = frappe.db.sql("""select modified from `tabMaterial Request` where name = %s""",
			self.name)
		date_diff = frappe.db.sql("""select TIMEDIFF('%s', '%s')"""
			% (mod_db[0][0], cstr(self.modified)))

		if date_diff and date_diff[0][0]:
			frappe.throw(_("{0} {1} has been modified. Please refresh.").format(_(self.doctype), self.name))

	def update_status(self, status):
		self.check_modified_date()
		self.status_can_change(status)
		self.set_status(update=True, status=status)
		self.update_requested_qty()

	def status_can_change(self, status):
		"""
		validates that `status` is acceptable for the present controller status
		and throws an Exception if otherwise.
		"""
		if self.status and self.status == 'Cancelled':
			# cancelled documents cannot change
			if status != self.status:
				frappe.throw(
					_("{0} {1} is cancelled so the action cannot be completed").
						format(_(self.doctype), self.name),
					frappe.InvalidStatusError
				)

		elif self.status and self.status == 'Draft':
			# draft document to pending only
			if status != 'Pending':
				frappe.throw(
					_("{0} {1} has not been submitted so the action cannot be completed").
						format(_(self.doctype), self.name),
					frappe.InvalidStatusError
				)

	def on_cancel(self):
		self.update_requested_qty()

	def update_completed_qty(self, mr_items=None, update_modified=True):
		if not mr_items:
			mr_items = [d.name for d in self.get("items")]

		for d in self.get("items"):
			if d.name in mr_items:
				if self.material_request_type in ("Material Issue", "Material Transfer"):
					d.ordered_qty =  flt(frappe.db.sql("""select sum(transfer_qty)
						from `tabStock Entry Detail` where material_request = %s
						and material_request_item = %s and docstatus = 1""",
						(self.name, d.name))[0][0])

					if d.ordered_qty and d.ordered_qty > d.stock_qty:
						frappe.throw(_("The total Issue / Transfer quantity {0} in Material Request {1}  \
							cannot be greater than requested quantity {2} for Item {3}").format(d.ordered_qty, d.parent, d.qty, d.item_code))

				elif self.material_request_type == "Manufacture":
					d.ordered_qty = flt(frappe.db.sql("""select sum(qty)
						from `tabProduction Order` where material_request = %s
						and material_request_item = %s and docstatus = 1""",
						(self.name, d.name))[0][0])

				frappe.db.set_value(d.doctype, d.name, "ordered_qty", d.ordered_qty)

		target_ref_field = 'qty' if self.material_request_type == "Manufacture" else 'stock_qty'
		self._update_percent_field({
			"target_dt": "Material Request Item",
			"target_parent_dt": self.doctype,
			"target_parent_field": "per_ordered",
			"target_ref_field": target_ref_field,
			"target_field": "ordered_qty",
			"name": self.name,
		}, update_modified)

	def update_requested_qty(self, mr_item_rows=None):
		"""update requested qty (before ordered_qty is updated)"""
		item_wh_list = []
		for d in self.get("items"):
			if (not mr_item_rows or d.name in mr_item_rows) and [d.item_code, d.warehouse] not in item_wh_list \
					and frappe.db.get_value("Item", d.item_code, "is_stock_item") == 1 and d.warehouse:
				item_wh_list.append([d.item_code, d.warehouse])

		for item_code, warehouse in item_wh_list:
			update_bin_qty(item_code, warehouse, {
				"indented_qty": get_indented_qty(item_code, warehouse)
			})

def update_completed_and_requested_qty(stock_entry, method):
	if stock_entry.doctype == "Stock Entry":
		material_request_map = {}

		for d in stock_entry.get("items"):
			if d.material_request:
				material_request_map.setdefault(d.material_request, []).append(d.material_request_item)

		for mr, mr_item_rows in material_request_map.items():
			if mr and mr_item_rows:
				mr_obj = frappe.get_doc("Material Request", mr)

				if mr_obj.status in ["Stopped", "Cancelled"]:
					frappe.throw(_("{0} {1} is cancelled or stopped").format(_("Material Request"), mr),
						frappe.InvalidStatusError)

				mr_obj.update_completed_qty(mr_item_rows)
				mr_obj.update_requested_qty(mr_item_rows)

def set_missing_values(source, target_doc):
	target_doc.run_method("set_missing_values")

def update_item(obj, target, source_parent):
	target.qty = flt(flt(obj.stock_qty) - flt(obj.ordered_qty))
	target.stock_qty = target.qty

def check_for_closed_status(doctype, docname):
	status = frappe.db.get_value(doctype, docname, "status")

	if status == "Closed":
		frappe.throw(_("{0} {1} status is {2}").format(doctype, docname, status), frappe.InvalidStatusError)

@frappe.whitelist()
def make_stock_entry(source_name, target_doc=None):
	def update_item(obj, target, source_parent):
		qty = flt(flt(obj.stock_qty) - flt(obj.ordered_qty)) \
			if flt(obj.stock_qty) > flt(obj.ordered_qty) else 0
		target.qty = qty
		target.transfer_qty = qty

		if source_parent.material_request_type == "Material Transfer":
			target.t_warehouse = obj.warehouse
		else:
			target.s_warehouse = obj.warehouse

	def set_missing_values(source, target):
		target.purpose = source.material_request_type

	doclist = get_mapped_doc("Material Request", source_name, {
		"Material Request": {
			"doctype": "Stock Entry",
			"validation": {
				"docstatus": ["=", 1],
				"material_request_type": ["in", ["Material Transfer", "Material Issue"]]
			}
		},
		"Material Request Item": {
			"doctype": "Stock Entry Detail",
			"field_map": {
				"name": "material_request_item",
				"parent": "material_request",
			},
			"postprocess": update_item,
			"condition": lambda doc: doc.ordered_qty < doc.stock_qty
		}
	}, target_doc, set_missing_values)

	return doclist

@frappe.whitelist()
def raise_production_orders(material_request):
	mr= frappe.get_doc("Material Request", material_request)
	errors =[]
	production_orders = []
	default_wip_warehouse = frappe.db.get_single_value("Manufacturing Settings", "default_wip_warehouse")
	for d in mr.items:
		if (d.qty - d.ordered_qty) >0:
			if frappe.db.get_value("BOM", {"item": d.item_code, "is_default": 1}):
				prod_order = frappe.new_doc("Production Order")
				prod_order.production_item = d.item_code
				prod_order.qty = d.qty - d.ordered_qty
				prod_order.fg_warehouse = d.warehouse
				prod_order.wip_warehouse = default_wip_warehouse
				prod_order.description = d.description
				prod_order.expected_delivery_date = d.schedule_date
				prod_order.bom_no = get_item_details(d.item_code).bom_no
				prod_order.material_request = mr.name
				prod_order.material_request_item = d.name
				prod_order.planned_start_date = mr.transaction_date
				prod_order.save()
				production_orders.append(prod_order.name)
			else:
				errors.append(_("Row {0}: Bill of Materials not found for the Item {1}").format(d.idx, d.item_code))
	if production_orders:
		message = ["""<a href="#Form/Production Order/%s" target="_blank">%s</a>""" % \
			(p, p) for p in production_orders]
		msgprint(_("The following Production Orders were created:") + '\n' + new_line_sep(message))
	if errors:
		frappe.throw(_("Productions Orders cannot be raised for:") + '\n' + new_line_sep(errors))
	return production_orders
