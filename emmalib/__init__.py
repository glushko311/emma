import sys
import os
import time
import re
import gc
import pickle

import gtk
from gtk import keysyms
import gobject
import gtk.gdk
import gtk.glade

from emmalib import __file__ as emmalib_file
from emmalib.mysql_host import *
from emmalib.mysql_query_tab import *

version = "0.2"
new_instance = None

re_src_after_order = "(?:[ \r\n\t](?:limit.*|procedure.*|for update.*|lock in share mode.*|[ \r\n\t]*$))"
re_src_query_order = "(?is)(.*order[ \r\n\t]+by[ \r\n\t]+)(.*?)([ \r\n\t]*" + re_src_after_order + ")"

emma_path = os.path.dirname(emmalib_file)
#emma_path = os.path.dirname(emmalib.__file__)
icons_path = os.path.join(emma_path, "icons")

class Emma:
	def __init__(self):
		self.created_once = {}
		self.glade_file = os.path.join(emma_path, "emma.glade")
		if not os.access(self.glade_file, os.R_OK):
			print self.glade_file, "not found!"
			sys.exit(-1)
		
		self.xml = gtk.glade.XML(self.glade_file)
		self.mainwindow = self.xml.get_widget("mainwindow")
		self.mainwindow.connect('destroy', lambda *args: gtk.main_quit())
		self.xml.signal_autoconnect(self)
		
		self.load_icons()
		
		# setup sql_log
		self.sql_log_model = gtk.ListStore(gobject.TYPE_STRING, gobject.TYPE_STRING)
		self.sql_log_tv = self.xml.get_widget("sql_log_tv")
		self.sql_log_tv.set_model(self.sql_log_model)
		self.sql_log_tv.append_column(gtk.TreeViewColumn("time", gtk.CellRendererText(), text=0))
		self.sql_log_tv.append_column(gtk.TreeViewColumn("query", gtk.CellRendererText(), markup=1))
		if hasattr(self, "state"):
			for log in self.state["sql_logs"]:
				self.sql_log_model.append(log)
		
		# setup msg
		self.msg_model = gtk.ListStore(gobject.TYPE_STRING, gobject.TYPE_STRING)
		self.msg_tv = self.xml.get_widget("msg_tv")
		self.msg_tv.set_model(self.msg_model)
		self.msg_tv.append_column(gtk.TreeViewColumn("time", gtk.CellRendererText(), text=0))
		self.msg_tv.append_column(gtk.TreeViewColumn("message", gtk.CellRendererText(), text=1))
		
		self.blob_tv = self.xml.get_widget("blob_tv")
		self.blob_tv.set_sensitive(False)
		self.blob_buffer = self.blob_tv.get_buffer()
		self.blob_view_visible = False
		
		# setup connections
		self.connections_model = gtk.TreeStore(gobject.TYPE_PYOBJECT);
		self.connections_tv = self.xml.get_widget("connections_tv")
		self.connections_tv.set_model(self.connections_model)
		col = gtk.TreeViewColumn("MySQL-Hosts")
		
		pixbuf_renderer = gtk.CellRendererPixbuf()
		col.pack_start(pixbuf_renderer, False)
		#col.add_attribute(pixbuf_renderer, "pixbuf", 1)
		col.set_cell_data_func(pixbuf_renderer, self.render_connections_pixbuf)
		
		text_renderer = gtk.CellRendererText()
		col.pack_end(text_renderer)
		#col.add_attribute(text_renderer, "text", 2)
		col.set_cell_data_func(text_renderer, self.render_connections_text)
		
		self.connections_tv.append_column(col)
		self.connections_tv.connect("row-expanded", self.on_row_expanded)
		self.connections_tv.connect("row-collapsed", self.on_row_collapsed)
		#connections_tv.insert_column_with_data_func(-1, "MySQL-Hosts", col)
		
		# processlist
		self.processlist_tv = self.xml.get_widget("processlist_treeview")
		self.processlist_model = None
		
		
		self.local_search_window = self.xml.get_widget("localsearch_window")
		self.local_search_entry = self.xml.get_widget("local_search_entry")
		self.local_search_entry.connect("activate", lambda *a: self.local_search_window.response(gtk.RESPONSE_OK));
		self.local_search_start_at_first_row = self.xml.get_widget("search_start_at_first_row")
		self.local_search_case_sensitive = self.xml.get_widget("search_case_sensitive")

		self.clipboard = gtk.Clipboard(gtk.gdk.display_get_default(), "CLIPBOARD")
		self.pri_clipboard = gtk.Clipboard(gtk.gdk.display_get_default(), "PRIMARY")
		
		self.field_edit = self.xml.get_widget("field_edit")
		self.field_edit_content = self.xml.get_widget("edit_field_content")

		self.table_property_labels = []
		self.table_property_entries = []
		self.table_description_size = (0, 0)
		self.table_description = self.xml.get_widget("table_description")
		
		self.query_notebook = self.xml.get_widget("query_notebook")
		
		self.tooltips = gtk.Tooltips()
		self.sort_timer_running = False
		self.field_conditions_initialized = False
		self.current_host = None
		self.current_processlist_host = None
		self.processlist_timer_running = False
		
		if not hasattr(self, "state"):
			self.hosts = {}
			self.load_config()
			self.queries = []
			self.add_query_tab(mysql_query_tab(self.xml, self.query_notebook))
		else:
			self.hosts = self.state["hosts"]
			self.load_config(True)
			self.queries = []
			first = True
			for q in self.state["queries"]:
				if first:
					xml = self.xml
				else:
					xml = gtk.glade.XML(self.glade_file, "first_query")
					
				new_page = xml.get_widget("first_query")
				q.__init__(xml, self.query_notebook)
				self.add_query_tab(q)
				
				if first:
					first = False
					self.query_notebook.set_tab_label_text(new_page, q.name)
				else:
					label = gtk.Label(q.name)
					label.show()
					self.query_notebook.append_page(new_page, label)
		
		if int(self.config["ping_connection_interval"]) > 0:
			gobject.timeout_add(
				int(self.config["ping_connection_interval"]) * 1000,
				self.on_connection_ping
			)
		
	def __getstate__(self):
		hosts = []
		iter = self.connections_model.get_iter_root()
		while iter:
			host = self.connections_model.get_value(iter, 0)
			hosts.append(host)
			iter = self.connections_model.iter_next(iter)
		
		sql_logs = []
		iter = self.sql_log_model.get_iter_root()
		while iter:
			log = self.sql_log_model.get(iter, 0, 1)
			sql_logs.append(log)
			iter = self.sql_log_model.iter_next(iter)
		
		return {"hosts": hosts, "queries": self.queries, "sql_logs": sql_logs}
		
	def add_query_tab(self, qt):
		self.current_query = qt
		self.queries.append(qt)
		qt.set_query_font(self.config["query_text_font"])
		qt.set_result_font(self.config["query_result_font"])
		if self.config_get_bool("query_text_wrap"):
			qt.set_wrap_mode(gtk.WRAP_WORD)
		else:
			qt.set_wrap_mode(gtk.WRAP_NONE)

	def del_query_tab(self, qt):
		if self.current_query == qt:
			self.current_query = None
			
		i = self.queries.index(qt)
		del self.queries[i]

	def on_connection_ping(self):
		iter = self.connections_model.get_iter_root()
		while iter:
			host = self.connections_model.get_value(iter, 0)
			if host.connected:
				print "pinging %s" % host.name,
				if not host.ping():
					print "...error! reconnect seems to fail!"
				else:
					print "ok"
			iter = self.connections_model.iter_next(iter)
		return True
		
	def search_query_end(self, text, start):
		try:	r = self.query_end_re
		except: r = self.query_end_re = re.compile(r'("(?:[^\\]|\\.)*?")|(\'(?:[^\\]|\\.)*?\')|(`(?:[^\\]|\\.)*?`)|(;)')
		while 1:
			result = re.search(r, text[start:])
			if not result: 
				return None
				
			start += result.end()
			if result.group(4):
				return start
				
	def is_query_editable(self, query, result = None):
		table, where, field, value, row_iter = self.get_unique_where(query)
		if not table or not where:
			return False
		return True
		
	def is_query_appendable(self, query):
		if not self.current_host: return False
		try:	r = self.query_select_re
		except: r = self.query_select_re = re.compile(r'(?i)("(?:[^\\]|\\.)*?")|(\'(?:[^\\]|\\.)*?\')|(`(?:[^\\]|\\.)*?`)|(union)|(select[ \r\n\t]+(.*)[ \r\n\t]+from[ \r\n\t]+(.*))')
		start = 0
		while 1:
			result = re.search(r, query[start:])
			if not result: 
				return False
			start += result.end()
			if result.group(4):
				return False # union
			if result.group(5) and result.group(6) and result.group(7):
				break # found select
		return result
	
	def read_expression(self, query):
		try:	r = self.query_expr_re
		except: r = self.query_expr_re = re.compile(
			r'(?is)("(?:[^\\]|\\.)*?")|(\'(?:[^\\]|\\.)*?\')|(`(?:[^\\]|\\.)*?`)|([^ \r\n\t]*[ \r\n\t]*\()|(\))|([0-9]+(?:\\.[0-9]*)?)|([^ \r\n\t,()"\'`]+)|(,)')
		# print "read expr in", query
		match = re.search(r, query)
		#if match: print match.groups()
		if not match: return (None, None)
		for i in range(1, match.lastindex + 1):
			if match.group(i): 
				t = match.group(i)
				e = match.end(i)
				current_token = t
				while current_token[len(current_token) - 1] == "(":
					# print "continuing recursion with", query[e:]
					exp, end = self.read_expression(query[e:])
					if not exp: break
					e += end
					t += " " + exp
					if exp == ")": break
									
				return (t, e)
		print "should not happen!"
		return (None, None)
		
	def get_order_from_query(self, query, return_before_and_after=False):
		current_order = []
		try:	r = self.query_order_re
		except: r = self.query_order_re = re.compile(re_src_query_order)
		# get current order by clause
		match = re.search(r, query)
		if not match: 
			print "no order found in", query
			return current_order
		before, order, after = match.groups()
		order.lower()
		start = 0
		while 1:
			item = []
			while 1:
				ident, end = self.read_expression(order[start:])
				if not ident: break
				if ident == ",": break
				if ident[0] == "`":
					ident = ident[1:-1]
				item.append(ident)
				start += end
			l = len(item)
			if l == 0: break
			elif l == 1: item.append(True)
			elif l == 2: 
				if item[1].lower() == "asc": 
					item[1] = True
				else:
					item[1] = False
			else:
				print "unknown order item:", item, "ignoring..."
				item = None
			if item: current_order.append(item)
			if not ident: break
			start += 1 # comma
		return current_order
		
	def get_field_list(self, s):
		# todo USE IT!
		fields = []
		start = 0
		while 1:
			item = []
			while 1:
				ident, end = self.read_expression(s[start:])
				if not ident: break
				if ident == ",": break
				if ident[0] == "`":
					ident = ident[1:-1]
				item.append(ident)
				start += end
			if len(item) == 1:
				fields.append(item[0])
			else:
				fields.append(item)
			if not ident: break
		print "found fields:", fields
		return fields
		
	def escape_fieldname(self, field):
		if not re.search("[` ]", field):
			return field
		return "`%s`" % field.replace("`", r"\`")
		
	def get_unique_where(self, query, path = None, col_num = None):
		# call is_query_appendable before!
		result = self.is_query_appendable(query)
		if not result:
			return (None, None, None, None, None)

		field_list = result.group(6)
		table_list = result.group(7)
		
		# check tables
		table_list = table_list.replace(" join ", ",")
		table_list = re.sub("(?i)(?:order[ \t\r\n]by.*|limit.*|group[ \r\n\t]by.*|order[ \r\n\t]by.*|where.*)", "", table_list)
		table_list = table_list.replace("`", "")
		tables = table_list.split(",")
		
		if len(tables) > 1:
			print "sorry, i can't edit queries with more than one than one source-table:", tables
			return (None, None, None, None, None)
	
		# get table_name
		table = tables[0].strip(" \r\n\t").strip("`'\"")
		print "table:", table
		
		# check for valid fields
		field_list = re.sub("[\r\n\t ]+", " ", field_list)
		field_list = re.sub("'.*?'", "__BAD__STRINGLITERAL", field_list)
		field_list = re.sub("\".*?\"", "__BAD__STRINGLITERAL", field_list)
		field_list = re.sub("\\(.*?\\)", "__BAD__FUNCTIONARGUMENTS", field_list)
		field_list = re.sub("\\|", "__PIPE__", field_list)
		temp_fields = field_list.split(",")
		fields = []
		for f in temp_fields:
			fields.append(f.strip("` \r\n\t"))
		print "fields:", fields
		
		wildcard = False
		for field in fields:
			if field.find("*") != -1:
				wildcard = True
				break;
		
		# find table handle!
		tries = 0
		new_tables = []
		while 1:
			try:
				th = self.current_host.current_db.tables[table]
				break
			except:
				tries += 1
				if tries > 1:
					print "query not editable, because table '%s' is not found in db %s" % (table, self.current_host.current_db)
					return (None, None, None, None, None)
				new_tables = self.current_host.current_db.refresh()
				continue
		# does this field really exist in this table?
		c = 0
		possible_primary = possible_unique = ""
		unique = primary = "";
		pri_okay = uni_okay = 0
		
		for i in new_tables:
			self.current_host.current_db.tables[i].refresh(False)
			
		if not th.fields and not table in new_tables:
			th.refresh(False)
		
		row_iter = None
		if path:
			row_iter = self.current_query.model.get_iter(path)
			
		# get unique where_clause
		
		for field, field_pos in zip(th.field_order, range(len(th.field_order))):
			props = th.fields[field]
			if pri_okay >= 0 and props[3] == "PRI":
				if possible_primary: possible_primary += ", "
				possible_primary += field
				if wildcard:
					c = field_pos
				else:
					c = None
					try:
						c = fields.index(field)
					except:
						pass
				if not c is None:
					pri_okay = 1
					if path:
						value = self.current_query.model.get_value(row_iter, c)
						if primary: primary += " and "
						primary += "`%s`='%s'" % (field, value)
			if uni_okay >= 0 and props[3] == "UNI":
				if possible_unique: possible_unique += ", "
				possible_unique += field
				if wildcard:
					c = field_pos
				else:
					c = None
					try:
						c = fields.index(field)
					except:
						pass
				if not c is None:
					uni_okay = 1
					if path:
						value = self.current_query.model.get_value(row_iter, c)
						if unique: unique += " and "
						unique += "`%s`='%s'" % (field, value)
						
		if uni_okay < 1 and pri_okay < 1:
			possible_key = "(i can't see any key-fields in this table...)"
			if possible_primary:
				possible_key = "e.g.'%s' would be useful!" % possible_primary;
			elif possible_unique:
				possible_key = "e.g.'%s' would be useful!" % possible_unique;
			print "no edit-key found. try to name a key-field in your select-clause.", possible_key
			return (table, None, None, None, None)
		
		value = ""
		field = None
		if path:
			where = primary
			if not where: where = unique
			if not where: where = None
			if not col_num is None:
				value = self.current_query.model.get_value(row_iter, col_num)
				print col_num, fields
				if wildcard:
					field = th.field_order[col_num]
				else:
					field = fields[col_num]
		else:
			where = possible_primary + possible_unique
			
		# get current edited field and value by col_num
		#print "%s, %s, %s, %s, %s" % (table, where, field, value, row_iter)
		return (table, where, field, value, row_iter)
		
	def on_row_expanded(self, tv, iter, path):
		o = tv.get_model().get_value(iter, 0)
		if len(path) > 3: return
		o.expanded = True
		
	def on_row_collapsed(self, tv, iter, path):
		o = tv.get_model().get_value(iter, 0)
		if len(path) > 3: return
		o.expanded = False
		
	def on_remove_order_clicked(self, button):
		query = self.current_query.last_source
		try:	r = self.query_order_re
		except: r = self.query_order_re = re.compile(re_src_query_order)
		match = re.search(r, query)
		if not match: 
			return
		before, order, after = match.groups()
		new_query = re.sub("(?i)order[ \r\n\t]+by[ \r\n\t]+", "", before + after)
		self.current_query.set(new_query)
		self.sort_timer_running = False
		self.on_execute_query_clicked()
		
	def on_query_column_sort(self, column, col_num):
		query = self.current_query.last_source
		current_order = self.get_order_from_query(query)
		col = column.get_title().replace("__", "_")
		new_order = []
		for c, o in current_order:
			if c == col:
				if o:
					new_order.append([col, False])
				col = None
			else:
				new_order.append([c, o])
		if col:
			new_order.append([col, True])
		try:	r = self.query_order_re
		except: r = self.query_order_re = re.compile(re_src_query_order)
		match = re.search(r, query)
		if match: 
			before, order, after = match.groups()
			order = ""
			addition = ""
		else:
			match = re.search(re_src_after_order, query)
			if not match:
				before = query
				after = ""
			else:
				before = query[0:match.start()]
				after = match.group()
			addition = "\norder by\n\t"
		order = ""
		for col, o in new_order:
			if order: order += ",\n\t"
			order += self.escape_fieldname(col)
			if not o: order += " desc"
		if order:
			new_query = ''.join([before, addition, order, after])
		else:
			new_query = re.sub("(?i)order[ \r\n\t]+by[ \r\n\t]+", "", before + after)
		self.current_query.set(new_query)
	
		if self.config["result_view_column_sort_timeout"] <= 0:
			on_execute_query_clicked()
			
		new_order = dict(new_order)
		
		for col in self.current_query.treeview.get_columns():
			field_name = col.get_title().replace("__", "_")
			try:
				sort_col = new_order[field_name]
				col.set_sort_indicator(True)
				if sort_col: 
					col.set_sort_order(gtk.SORT_ASCENDING)
				else:
					col.set_sort_order(gtk.SORT_DESCENDING)
			except:
				col.set_sort_indicator(False)
			
		if not self.sort_timer_running:
			self.sort_timer_running = True
			gobject.timeout_add(
				100 + int(self.config["result_view_column_sort_timeout"]),
				self.on_sort_timer
			)
		self.sort_timer_execute = time.time() + int(self.config["result_view_column_sort_timeout"]) / 1000.
		
	def on_sort_timer(self):
		if not self.sort_timer_running:
			return False # aborted
		if self.sort_timer_execute > time.time():
			return True # new order -> wait again
		self.sort_timer_running = False
		self.on_execute_query_clicked()
		return False # done

	def on_query_change_data(self, cellrenderer, path, new_value, col_num):
		q = self.current_query
		row_iter = q.model.get_iter(path)
		if q.append_iter and q.model.iter_is_valid(q.append_iter) and q.model.get_path(q.append_iter) == q.model.get_path(row_iter):
			q.filled_fields[q.treeview.get_column(col_num).get_title().replace("__", "_")] = new_value
			q.model.set_value(row_iter, col_num, new_value)
			return
		
		table, where, field, value, row_iter = self.get_unique_where(q.last_source, path, col_num)
		if new_value == value:
			return
		update_query = "update `%s` set `%s`='%s' where %s limit 1" % (table, field, self.current_host.escape(new_value), where)
		if self.current_host.query(update_query):
			q.model.set_value(row_iter, col_num, new_value)
			return True
		return False
		
	def on_delete_record_tool_clicked(self, button):
		q = self.current_query
		path, column = q.treeview.get_cursor()
		if not path: return
		row_iter = q.model.get_iter(path)
		if q.append_iter and q.model.iter_is_valid(q.append_iter) and q.model.get_path(q.append_iter) == q.model.get_path(row_iter):
			q.append_iter = None
			q.apply_record.set_sensitive(False)
		else:
			table, where, field, value, row_iter = self.get_unique_where(q.last_source, path)
			if not table or not where:
				show_message("delete record", "could not delete this record!?")
				return
			update_query = "delete from `%s` where %s limit 1" % (table, where)
			if not self.current_host.query(update_query):
				return
		if not q.model.remove(row_iter):
			row_iter = q.model.get_iter_first()
			while row_iter:
				new = q.model.iter_next(row_iter)
				if new is None:
					break
				row_iter = new
		if row_iter:
			q.treeview.set_cursor(q.model.get_path(row_iter))
			
	def on_add_record_tool_clicked(self, button):
		q = self.current_query
		if not q.add_record.get_property("sensitive"):
			return
			
		path, column = q.treeview.get_cursor()
		if path:
			iter = q.model.insert_after(q.model.get_iter(path))
		else:
			iter = q.model.append()
		q.treeview.grab_focus()
		q.treeview.set_cursor(q.model.get_path(iter))
		q.filled_fields = dict()
		q.append_iter = iter
		q.apply_record.set_sensitive(True)
		
	def on_apply_record_tool_clicked(self, button):
		q = self.current_query
		if not q.append_iter:
			return
		query = ""
		for field, value in q.filled_fields.iteritems():
			if query: query += ", "
			query += "%s='%s'" % (self.escape_fieldname(field), self.current_host.escape(value))
		if query: 
			table, where, field, value, row_iter = self.get_unique_where(q.last_source)
			update_query = "insert into `%s` set %s" % (table, query)
			if not self.current_host.query(update_query):
				return False
				
			# todo: use insert_id() to retrieve the complete inserted record and set correct field values!
		else:
			q.model.remove(q.append_iter)
		q.append_iter = None
		q.apply_record.set_sensitive(False)
		return True
		
	def on_message_notebook_switch_page(self, nb, pointer, page):
		self.blob_view_visible = (page == 2)
		if self.blob_view_visible:
			self.on_query_view_cursor_changed(self.current_query.treeview)
		
	def on_query_view_cursor_changed(self, tv):
		q = self.current_query
		path, column = q.treeview.get_cursor()
		
		if not path:
			return
		
		if self.blob_view_visible and column:
			iter = q.model.get_iter(path)
			col = q.treeview.get_columns().index(column)
			value = q.model.get_value(iter, col)
			if value is None:
				self.blob_buffer.set_text("")
			else:
				self.blob_buffer.set_text(value)
			self.blob_tv.set_sensitive(True)
		else:
			self.blob_buffer.set_text("")
			self.blob_tv.set_sensitive(False)
		
		if q.append_iter:
			if path == q.model.get_path(q.append_iter):
				return
			self.on_apply_record_tool_clicked(None)
		
	def on_execute_query_clicked(self, button = None, query = None):
		if not self.current_query: return
		q = self.current_query
		if not query:
			b = q.textview.get_buffer()
			text = b.get_text(b.get_start_iter(), b.get_end_iter())
		else:
			text = query
		host = self.current_host
		
		if q.db:
			host = q.db.host
			host.select_database(q.db)
	
		if not host:
			self.show_message(
				"error executing this query!",
				"could not execute query, because there is no selected host!"
			)
			return

		update = False
		select = False
		q.editable = False
		# single popup
		self.xml.get_widget("add_record").set_sensitive(False)
		self.xml.get_widget("delete_record").set_sensitive(False)
		# per query buttons
		q.add_record.set_sensitive(False)
		q.delete_record.set_sensitive(False)
		q.apply_record.set_sensitive(False)
		q.local_search.set_sensitive(False)
		q.remove_order.set_sensitive(False)
		
		affected_rows = 0
		last_insert_id = 0
		num_rows = 0
		num_fields = 0
		
		query_time = 0
		download_time = 0
		display_time = 0
		query_count = 0
		total_start = time.time();
		
		# cleanup last query model and treeview
		for col in q.treeview.get_columns():
			q.treeview.remove_column(col)
		if q.model: q.model.clear()
		
		start = 0
		while start < len(text):
			query_start = start
			# search query end
			end = self.search_query_end(text, start)
			if not end:
				thisquery = text[start:]
				start = len(text);
			else:
				thisquery = text[start:end]
				start += end + 1;
				
			thisquery.strip(" \r\n\t;")
			if not thisquery: continue # empty query
			
			query_count += 1
			q.label.set_text("executing query %d..." % query_count)
			q.label.window.process_updates(False)
			
			appendable = False
			appendable_result = self.is_query_appendable(thisquery)
			if appendable_result:
				appendable = True
				q.editable = self.is_query_editable(thisquery, appendable_result)
			print "appendable: %s, editable: %s" % (appendable, q.editable)
			
			ret = host.query(thisquery)
			query_time += host.query_time
			
			# if stop on error is enabled
			if not ret:
				message = "error at: %s" % host.last_error.replace("You have an error in your SQL syntax.  Check the manual that corresponds to your MySQL server version for the right syntax to use near ", "")
				i = q.textview.get_buffer().get_iter_at_offset(query_start)
				
				match = re.match("error at: '(.*?)'.*", message)
				if match:
					# set focus and cursor!
					pos = text.find(match.group(1), query_start, query_start + len(thisquery))
					if not pos == -1:
						i.set_offset(query_start + pos);
				else:
					match = re.match("Unknown column '(.*?')", message)
					if match:
						# set focus and cursor!
						pos = thisquery.find(match.group(1))
						if not pos == 1:
							i.set_offset(query_start + pos);
							
				q.textview.get_buffer().place_cursor(i)
				q.textview.scroll_to_iter(i, 0.0)
				q.textview.grab_focus()
				q.label.set_text(re.sub("[\r\n\t ]+", " ", message))
				return
			
			field_count = host.handle.field_count()
			if field_count == 0:
				# query without result
				update = True;
				affected_rows += host.handle.affected_rows()
				last_insert_id = host.handle.insert_id()
				continue
		
			# query with result
			q.append_iter = None
			q.local_search.set_sensitive(True)
			q.add_record.set_sensitive(appendable)
			q.delete_record.set_sensitive(q.editable)
			select = True
			q.last_source = thisquery
			# get sort order!
			sortable = True # todo
			current_order = self.get_order_from_query(thisquery)
			sens = False
			if len(current_order) > 0: sens = True
			q.remove_order.set_sensitive(sens and sortable)
			
			sort_fields = dict()
			for c, o in current_order:
				sort_fields[c.lower()] = o
			q.label.set_text("downloading resultset...")
			q.label.window.process_updates(False)
			
			start_download = time.time()
			result = host.handle.store_result()
			download_time = time.time() - start_download
			if download_time < 0: download_time = 0
	
			q.label.set_text("displaying resultset...");
			q.label.window.process_updates(False)
			
			# store field info
			q.result_info = result.describe()
			num_rows = result.num_rows()
			
			for col in q.treeview.get_columns():
				q.treeview.remove_column(col)
				
			columns = [gobject.TYPE_STRING] * field_count
			q.model = gtk.ListStore(*columns)
			q.treeview.set_model(q.model)
			q.treeview.set_rules_hint(True)
			q.treeview.set_headers_clickable(True)
			for i in range(field_count):
				title = q.result_info[i][0].replace("_", "__").replace("[\r\n\t ]+", " ")
				text_renderer = gtk.CellRendererText()
				if q.editable:
					text_renderer.set_property("editable", True)
					text_renderer.connect("edited", self.on_query_change_data, i)
				l = q.treeview.insert_column_with_data_func(-1, title, text_renderer, self.render_mysql_string, i)
				
				col = q.treeview.get_column(l - 1)
				
				if self.config_get_bool("result_view_column_resizable"): 
					col.set_resizable(True)
				else:
					col.set_resizable(False);
					col.set_min_width(int(self.config["result_view_column_width_min"]))
					col.set_max_width(int(self.config["result_view_column_width_max"]))
				
				if sortable:
					col.set_clickable(True)
					col.connect("clicked", self.on_query_column_sort, i)
					# set sort indicator
					field_name = q.result_info[i][0].lower()
					try:
						sort_col = sort_fields[field_name]
						col.set_sort_indicator(True)
						if sort_col: 
							col.set_sort_order(gtk.SORT_ASCENDING)
						else:
							col.set_sort_order(gtk.SORT_DESCENDING)
					except:
						col.set_sort_indicator(False)
				else:
					col.set_clickable(False)
					col.set_sort_indicator(False)
					
			cnt = 0
			start_display = time.time()
			last_display = start_display
			for row in result.fetch_row(0):
				q.model.append(row)
				
				cnt += 1;
				if not cnt % 100 == 0: continue
					
				now = time.time()
				if (now - last_display) < 0.2: continue
					
				q.label.set_text("displayed %d rows..." % cnt)
				q.label.window.process_updates(False)
				last_display = now
			
			display_time = time.time() - start_display
			if display_time < 0: display_time = 0
		
		result = []
		if select:
			# there was a query with a result
			result.append("rows: %d" % num_rows)
			result.append("fields: %d" % field_count)
		if update:
			# there was a query without a result
			result.append("affected rows: %d" % affected_rows)
			result.append("insert_id: %d" % last_insert_id)
		total_time = time.time() - total_start
		result.append("| total time: %.2fs (query: %.2fs" % (total_time, query_time))
		if select:
			result.append("download: %.2fs display: %.2fs" % (download_time, display_time))
		result.append(")")
	
		q.label.set_text(' '.join(result))
		# todo update_buttons();	
		gc.collect()
		
	def on_save_result_clicked(self, button):
		if not self.current_query:
			return
		
		d = self.assign_once("save results dialog", 
			gtk.FileChooserDialog, "save results", self.mainwindow, gtk.FILE_CHOOSER_ACTION_SAVE, 
				(gtk.STOCK_CANCEL, gtk.RESPONSE_REJECT, gtk.STOCK_SAVE, gtk.RESPONSE_ACCEPT))
		
		d.set_default_response(gtk.RESPONSE_ACCEPT)
		answer = d.run()
		d.hide()
		if not answer == gtk.RESPONSE_ACCEPT: return
		filename = d.get_filename()
		if os.path.exists(filename):
			if not os.path.isfile(filename):
				self.show_message("save results", "%s already exists and is not a file!" % filename)
				return
			if not self.confirm("overwrite file?", "%s already exists! do you want to overwrite it?" % filename):
				return
		q = self.current_query
		iter = q.model.get_iter_first()
		indices = range(q.model.get_n_columns())
		field_delim = self.config["save_result_as_csv_delim"]
		line_delim = self.config["save_result_as_csv_line_delim"]
		try:
			fp = file(filename, "wb")
			for search, replace in {"\\n": "\n", "\\r": "\r", "\\t": "\t", "\\0": "\0"}.iteritems():
				field_delim = field_delim.replace(search, replace)
				line_delim = line_delim.replace(search, replace)
			while iter:
				row = q.model.get(iter, *indices)
				for field in row:
					value = field
					if value is None: value = ""
					fp.write(value.replace(field_delim, "\\" + field_delim))
					fp.write(field_delim)
				fp.write(line_delim)
				iter = q.model.iter_next(iter)
			fp.close()
		except:
			self.show_message("save results", "error writing query to file %s: %s" % (filename, sys.exc_value))
		
		
	def assign_once(self, name, creator, *args):
		try:
			return self.created_once[name]
		except:
			obj = creator(*args)
			self.created_once[name] = obj
			return obj
		
	def on_save_query_clicked(self, button):
		if not self.current_query:
			return
		
		d = self.assign_once("save dialog", 
			gtk.FileChooserDialog, "save query", self.mainwindow, gtk.FILE_CHOOSER_ACTION_SAVE, 
				(gtk.STOCK_CANCEL, gtk.RESPONSE_REJECT, gtk.STOCK_SAVE, gtk.RESPONSE_ACCEPT))
		
		d.set_default_response(gtk.RESPONSE_ACCEPT)
		answer = d.run()
		d.hide()
		if not answer == gtk.RESPONSE_ACCEPT: return
		filename = d.get_filename()
		if os.path.exists(filename):
			if not os.path.isfile(filename):
				self.show_message("save query", "%s already exists and is not a file!" % filename)
				return
			if not self.confirm("overwrite file?", "%s already exists! do you want to overwrite it?" % filename):
				return
		b = self.current_query.textview.get_buffer()
		query_text = b.get_text(b.get_start_iter(), b.get_end_iter())
		try:
			fp = file(filename, "wb")
			fp.write(query_text)
			fp.close()
		except:
			self.show_message("save query", "error writing query to file %s: %s" % (filename, sys.exc_value))
		
	def on_load_query_clicked(self, button):
		if not self.current_query:
			return
		
		d = self.assign_once("load dialog", 
			gtk.FileChooserDialog, "load query", self.mainwindow, gtk.FILE_CHOOSER_ACTION_OPEN, 
				(gtk.STOCK_CANCEL, gtk.RESPONSE_REJECT, gtk.STOCK_OPEN, gtk.RESPONSE_ACCEPT))
		
		d.set_default_response(gtk.RESPONSE_ACCEPT)
		answer = d.run()
		d.hide()
		if not answer == gtk.RESPONSE_ACCEPT: return
		filename = d.get_filename()
		if not os.path.exists(filename):
			self.show_message("load query", "%s does not exists!" % filename)
			return
		if not os.path.isfile(filename):
			self.show_message("load query", "%s exists, but is not a file!" % filename)
			return
			
		try:
			fp = file(filename, "rb")
			query_text = fp.read()
			fp.close()
		except:
			self.show_message("save query", "error writing query to file %s: %s" % (filename, sys.exc_value))
			return
		self.current_query.textview.get_buffer().set_text(query_text)
		
	def on_save_workspace_activate(self, button):
		d = self.assign_once("save workspace dialog", 
			gtk.FileChooserDialog, "save workspace", self.mainwindow, gtk.FILE_CHOOSER_ACTION_SAVE, 
				(gtk.STOCK_CANCEL, gtk.RESPONSE_REJECT, gtk.STOCK_SAVE, gtk.RESPONSE_ACCEPT))
		
		d.set_default_response(gtk.RESPONSE_ACCEPT)
		answer = d.run()
		d.hide()
		if not answer == gtk.RESPONSE_ACCEPT: return
		filename = d.get_filename()
		if os.path.exists(filename):
			if not os.path.isfile(filename):
				self.show_message("save workspace", "%s already exists and is not a file!" % filename)
				return
			if not self.confirm("overwrite file?", "%s already exists! do you want to overwrite it?" % filename):
				return
		try:
			fp = file(filename, "wb")
			pickle.dump(self, fp)
			fp.close()
		except:
			self.show_message("save workspace", "error writing workspace to file %s: %s/%s" % (filename, sys.exc_type, sys.exc_value))
		
	def on_restore_workspace_activate(self, button):
		global new_instance
		d = self.assign_once("restore workspace dialog", 
			gtk.FileChooserDialog, "restore workspace", self.mainwindow, gtk.FILE_CHOOSER_ACTION_OPEN, 
				(gtk.STOCK_CANCEL, gtk.RESPONSE_REJECT, gtk.STOCK_OPEN, gtk.RESPONSE_ACCEPT))
		
		d.set_default_response(gtk.RESPONSE_ACCEPT)
		answer = d.run()
		d.hide()
		if not answer == gtk.RESPONSE_ACCEPT: return
		filename = d.get_filename()
		if not os.path.exists(filename):
			self.show_message("restore workspace", "%s does not exists!" % filename)
			return
		if not os.path.isfile(filename):
			self.show_message("restore workspace", "%s exists, but is not a file!" % filename)
			return
			
		try:
			fp = file(filename, "rb")
			print "i am unpickling:", self
			new_instance = pickle.load(fp)
			print "got new instance:", new_instance
			fp.close()
		except:
			self.show_message("restore workspace", "error restoring workspace from file %s: %s/%s" % (filename, sys.exc_type, sys.exc_value))
		self.mainwindow.destroy()

	def __setstate__(self, state):
		self.state = state
		
	def on_local_search_button_clicked(self, button, again = False):
		if not self.current_query.local_search.get_property("sensitive"):
			return
			
		query_view = self.current_query.treeview
		self.local_search_start_at_first_row.set_active(False)
		if not again or not self.local_search_entry.get_text():
			self.local_search_entry.grab_focus()
			answer = self.local_search_window.run()
			self.local_search_window.hide()
			if not answer == gtk.RESPONSE_OK: return
		regex = self.local_search_entry.get_text()
		if self.local_search_case_sensitive.get_active():
			regex = "(?i)" + regex;
		tm = self.current_query.model
		fields = tm.get_n_columns()
		start = tm.get_iter_root()
		start_column_index = -1
		start_path = None
		if not self.local_search_start_at_first_row.get_active():
			start_path, start_column = query_view.get_cursor()
			if start_path:
				start = tm.get_iter(start_path)
				for k in range(fields):
					if query_view.get_column(k) == start_column:
						start_column_index = k
						break
			else:
				start_path = None
		while start:
			for k in range(fields):
				v = tm.get_value(start, k)
				if v is None: continue
				if re.search(regex, v):
					path = tm.get_path(start);
					if start_path and start_path == path and k <= start_column_index:
						continue # skip!
					column = query_view.get_column(k)
					query_view.set_cursor(path, column)
					query_view.scroll_to_cell(path, column)
					query_view.grab_focus()
					return
			start = tm.iter_next(start)
		self.show_message("local regex search", "sorry, no match found!\ntry to search from the beginning or execute a less restrictive query...")
		
	def on_pretty_format_clicked(self, button):
		print __name__
		
	def on_compress_format_clicked(self, button):
		print __name__
		
	def on_query_font_clicked(self, button):
		d = self.assign_once("query text font", gtk.FontSelectionDialog, "select query font")
		d.set_font_name(self.config["query_text_font"])
		answer = d.run()
		d.hide()
		if not answer == gtk.RESPONSE_OK: return
		font_name = d.get_font_name()
		self.current_query.set_query_font(font_name)
		self.config["query_text_font"] = font_name
		self.save_config()

	def on_query_result_font_clicked(self, button):
		d = self.assign_once("query result font", gtk.FontSelectionDialog, "select result font")
		d.set_font_name(self.config["query_result_font"])
		answer = d.run()
		d.hide()
		if not answer == gtk.RESPONSE_OK: return
		font_name = d.get_font_name()
		self.current_query.set_result_font(font_name)
		self.config["query_result_font"] = font_name
		self.save_config()
		
	def on_newquery_button_clicked(self, button):
		xml = gtk.glade.XML(self.glade_file, "first_query")
		new_page = xml.get_widget("first_query")
		self.add_query_tab(mysql_query_tab(xml, self.query_notebook))
		label = gtk.Label("query%d" % len(self.queries))
		label.show()
		self.query_notebook.append_page(new_page, label)
		self.query_notebook.set_current_page(len(self.queries) - 1)
		self.current_query.textview.grab_focus()
		
	def on_query_notebook_switch_page(self, nb, pointer, page):
		self.current_query = self.queries[page]
	
	def on_closequery_button_clicked(self, button):
		if len(self.queries) == 1: return
		self.current_query.destroy()
		self.del_query_tab(self.current_query)
		self.query_notebook.remove_page(self.query_notebook.get_current_page())
		gc.collect()
		
	def on_rename_query_tab_clicked(self, button):
		new_name = self.input("rename tab", "please enter the new name of this tab:",
			self.query_notebook.get_tab_label_text(self.current_query.page))
		self.query_notebook.set_tab_label_text(self.current_query.page, new_name)
		
	def on_processlist_refresh_value_change(self, button):
		value = button.get_value()
		if self.processlist_timer_running: return
		self.processlist_timer_running = True
		self.processlist_timer_interval = value
		gobject.timeout_add(int(value * 1000), self.on_processlist_refresh_timeout, button)
		
	def on_fc_reset_clicked(self, button):
		for i in range(self.fc_count):
			self.fc_entry[i].set_text("")
			if i == 0:
				self.fc_combobox[i].set_active(0)
				self.fc_op_combobox[i].set_active(0)
			else:
				self.fc_combobox[i].set_active(-1)
				self.fc_op_combobox[i].set_active(-1)
			if i: self.fc_logic_combobox[i - 1].set_active(0)
	
	def on_quit_activate(self, item):
		print self, "quit"
		gtk.main_quit()
		
	def on_about_activate(self, item):
		aboutdialog = self.xml.get_widget("aboutdialog")
		aboutdialog.set_version(version)
		aboutdialog.run()
		aboutdialog.hide()

	def on_changelog_activate(self, item):
		fp = file("changelog")
		changelog = fp.read()
		fp.close()
		w = self.xml.get_widget("changelog_window")
		tv = self.xml.get_widget("changelog_text")
		tv.get_buffer().set_text(changelog)
		w.connect('delete-event', self.on_changelog_delete)
		w.show()
		
	def on_changelog_delete(self, window, event):
		window.hide()
		return True
		
	def on_kill_process(self, button):
		path, column = self.processlist_tv.get_cursor()
		if not path or not self.current_host: return
		iter = self.processlist_model.get_iter(path)
		process_id = self.processlist_model.get_value(iter, 0)
		if not self.current_host.query("kill %s" % process_id):
			self.show_message("sorry", "there was an error while trying to kill process_id %s!" % process_id)
	
	def on_sql_log_activate(self, *args):
		print args
		
	def on_sql_log_button_press(self, tv, event):
		if not event.button == 3: return False
		res = tv.get_path_at_pos(int(event.x), int(event.y));
		if not res: return False
		self.xml.get_widget("sqllog_popup").popup(None, None, None, event.button, event.time);
		return True
		
	def on_connections_button_release(self, tv, event):
		if not event.button == 3: return False
		res = tv.get_path_at_pos(int(event.x), int(event.y));
		menu = None
		if not res or len(res[0]) == 1:	
			self.xml.get_widget("modify_connection").set_sensitive(not not res)
			self.xml.get_widget("delete_connection").set_sensitive(not not res)
			connected_host = False
			if res:
				model = self.connections_model
				iter = model.get_iter(res[0])
				host = model.get_value(iter, 0)
				connected_host = host.connected
			self.xml.get_widget("new_database").set_sensitive(connected_host)
			self.xml.get_widget("refresh_host").set_sensitive(connected_host)
			menu = self.xml.get_widget("connection_menu")
		elif len(res[0]) == 2:
			menu = self.xml.get_widget("database_popup")
		elif len(res[0]) == 3:
			menu = self.xml.get_widget("table_popup")
		else: print "no popup at path depth %d\n" % res[0].size()
		if menu:
			menu.popup(None, None, None, event.button, event.time)
		return True
		
	def on_connections_tv_cursor_changed(self, tv):
		path, column = tv.get_cursor()
		nb = self.xml.get_widget("main_notebook")
		if len(path) == 3 and nb.get_current_page() == 3:
			self.update_table_view(path)
	
	def on_nb_change_page(self, np, pointer, page):
		if page == 2:
			self.redraw_tables()
			return
		path, column = self.connections_tv.get_cursor()
		if not path: return
		if len(path) == 3 and page == 3:
			self.update_table_view(path)
	
	def update_table_view(self, path = None):
		if not path:
			path, column = self.connections_tv.get_cursor()
			if len(path) != 3: return
		iter = self.connections_model.get_iter(path)
		th = self.connections_model.get_value(iter, 0)
		
		table = self.xml.get_widget("table_properties")
		prop_count = len(th.props)
		if len(self.table_property_labels) != prop_count:
			for c in self.table_property_labels:
				table.remove(c)
			for c in self.table_property_entries:
				table.remove(c)
			self.table_property_labels = []
			self.table_property_entries = []
			table.resize(prop_count, 2)
			r = 0
			for h, p in zip(th.db.status_headers, th.props):
				l = gtk.Label(h)
				l.set_alignment(0, 0.5)
				e = gtk.Entry()
				e.set_editable(False)
				if p is None: p = ""
				e.set_text(p)
				table.attach(l, 0, 1, r, r + 1, gtk.FILL, 0)
				table.attach(e, 1, 2, r, r + 1, gtk.EXPAND|gtk.FILL|gtk.SHRINK, 0)
				l.show()
				e.show()
				self.table_property_labels.append(l)
				self.table_property_entries.append(e)
				r += 1
		else:
			r = 0
			for h, p in zip(th.db.status_headers, th.props):
				l = self.table_property_labels[r]
				e = self.table_property_entries[r]
				l.set_label(h)
				if p is None: p = ""
				e.set_text(p)
				r += 1
				
		tv = self.xml.get_widget("table_textview")
		tv.get_buffer().set_text(th.get_create_table())

		t = self.table_description
		for c in t.get_children():
			self.table_description.remove(c)
		self.table_description.resize(len(th.describe_headers), len(th.fields) + 1)
		c = 0
		for h in th.describe_headers:
			l = gtk.Label(h)
			t.attach(l, c, c + 1, 0, 1, gtk.FILL, 0)
			l.show()
			c += 1
		r = 1
		for fn in th.field_order:
			v = th.fields[fn]
			for c in range(len(th.describe_headers)):
				s = v[c]
				if s is None: s = ""
				l = gtk.Label(s)
				t.attach(l, c, c + 1, r, r + 1, gtk.FILL, 0)
				l.set_alignment(0, 0.5)
				l.set_selectable(True)
				l.show()
			r += 1
		self.xml.get_widget("vbox14").check_resize()
		self.tables_count = 0
		self.redraw_tables()
		
	def on_connections_row_activated(self, tv, path, col):
		depth = len(path)
		iter = self.connections_model.get_iter(path)
		o = self.connections_model.get_value(iter, 0)
		
		nb = self.xml.get_widget("main_notebook")
		if depth == 1: # host
			self.current_host = host = o
			if host.connected:
				current_host = None
				host.close()
			else:
				host.connect()
				if not host.connected: return
				host.refresh()
				self.refresh_processlist()
				nb.set_current_page(1)
			self.redraw_host(host, iter, True)
			
		elif depth == 2: # database
			self.current_host = o.host
			new_tables = o.refresh()
			self.redraw_db(o, iter, new_tables, True)
			self.redraw_tables()
			# self.connections_tv.expand_row(path, False)
			# todo update_query_db()
			
		elif depth == 3: # table
			self.current_host = host = o.db.host
			host.select_database(o.db)
			table = o
			if not table.fields or (time.time() - table.last_field_read) > self.config["autorefresh_interval_table"]:
				table.refresh()
				self.redraw_table(o, iter)
				
			if self.first_template:
				nb.set_current_page(4)
				self.on_template(None, self.first_template)
			elif nb.get_current_page() < 3:
				nb.set_current_page(3)				
			
			#self.connections_tv.expand_row(path, False)
			# todo update_query_db();
			# todo if(!doubleclick) update_table(e, i)
		else:
			print "No Handler for tree-depth", depth
		return
		
	def on_mainwindow_key_release_event(self, window, event):
		#print "state: %d, keyval: 0x%04x, text: '%s'" % (event.state, event.keyval, event.string)
		
		#~ RefPtr<Gnome::Glade::Xml> xml = queries[current_query].xml;
		#~ xml_get_decl_widget_from(xml, local_search_button, Gtk::ToolButton);
			
		#~ if(event->keyval == GDK_Tab) {
			#~ return do_auto_completion();
		#~ } else 
		#if event.keyval == keysyms.F9 or (event.state == 4 and event.keyval == keysyms.Return):
		#	self.on_execute_query_clicked(None)
		#	return True
		#~ } else if(event->keyval == GDK_F6) {
			#~ query_notebook->set_current_page((current_query + 1) % queries.size());
			#~ return true;
		#~ } else if(event->state & 4 && event->keyval == GDK_t) {
			#~ new_query_tab();
			#~ return true;
		#~ } else if(event->state & 4 && event->keyval == GDK_w) {
			#~ close_query_tab();
			#~ return true;
		#~ } else if(event->state & 4 && event->keyval == GDK_s) {
			#~ on_save_query();
			#~ return true;
		#~ } else if(event->state & 4 && event->keyval == GDK_o) {
			#~ on_load_query();
			#~ return true;
		#~ } else if(event->state & 4 && event->keyval == GDK_p) {
			#~ on_pretty_format();
			#~ return true;
		if event.keyval == keysyms.F3:
			self.on_local_search_button_clicked(None, True)
			return True
		#~ } else if(event->state & 4 && event->keyval == GDK_u) {
			#~ xml_get_decl_widget_from(xml, query_text, Gtk::TextView);
			
			#~ TextBuffer::iterator start, end;
			#~ query_text->get_buffer()->get_selection_bounds(start, end);
			#~ string text = query_text->get_buffer()->get_text(start, end);
			#~ text = to_lower(text);
			#~ query_text->get_buffer()->erase_selection();
			#~ query_text->get_buffer()->get_selection_bounds(start, end);
			#~ query_text->get_buffer()->insert(start, text);
			#~ query_text->get_buffer()->get_selection_bounds(start, end);
			#~ start = end;
			#~ end.backward_chars(text.size());
			#~ query_text->get_buffer()->select_range(start, end);
			#~ return true;
		#~ } else if(event->state & 4 && event->keyval == GDK_U) {
			#~ xml_get_decl_widget_from(xml, query_text, Gtk::TextView);
			
			#~ TextBuffer::iterator start, end;
			#~ query_text->get_buffer()->get_selection_bounds(start, end);
			#~ string text = to_upper(query_text->get_buffer()->get_text(start, end));
			#~ query_text->get_buffer()->erase_selection();
			#~ query_text->get_buffer()->get_selection_bounds(start, end);
			#~ query_text->get_buffer()->insert(start, text);
			#~ query_text->get_buffer()->get_selection_bounds(start, end);
			#~ start = end;
			#~ end.backward_chars(text.size());
			#~ query_text->get_buffer()->select_range(start, end);
			
			#~ return true;
		#~ } else if(event->state & 4 && event->keyval >= GDK_0 && event->keyval <= GDK_9) {
			#~ int page = event->keyval - GDK_0;
			#~ if(page == 0) page = 10;
			#~ page--;
			#~ if(page < query_notebook->get_n_pages())
				#~ query_notebook->set_current_page(page);
			#~ // on_parse_query("vos2sql");
		#~ }
		#~ return false;
		
	def on_query_view_key_press_event(self, tv, event):
		q = self.current_query
		path, column = q.treeview.get_cursor()
		if event.keyval == keysyms.F2:
			q.treeview.set_cursor(path, column, True)
			return True
		
		iter = q.model.get_iter(path)
		if event.keyval == keysyms.Down and not q.model.iter_next(iter):
			if q.append_iter and not self.on_apply_record_tool_clicked(None):
				return True
			self.on_add_record_tool_clicked(None)
			return True
			
	def on_query_view_button_release_event(self, tv, event):
		if not event.button == 3: return False
		res = tv.get_path_at_pos(int(event.x), int(event.y));
		menu = self.xml.get_widget("result_popup")
		if res:
			sensitive = True
		else:
			sensitive = False
		for c in menu.get_children():
			for s in ["edit", "set ", "delete"]:
				if c.name.find(s) != -1:
					c.set_sensitive(sensitive and self.current_query.editable)
					break
			else:
				if c.name not in ["add_record"]:
					c.set_sensitive(sensitive)
				else:
					c.set_sensitive(self.current_query.add_record.get_property("sensitive"))
		#menu.popup(None, None, None, event.button, event.time)
		menu.popup(None, None, None, 0, event.time) # strange!
		return True
	
	def on_table_popup(self, item):
		path, column = self.connections_tv.get_cursor()
		iter = self.connections_model.get_iter(path)
		what = item.name
		table = self.connections_model.get_value(iter, 0)
		
		if what == "refresh_table":
			table.refresh()
			self.redraw_table(table, iter)
			self.update_table_view()
		elif what == "truncate_table":
			if not self.confirm("truncate table", "do you really want to truncate the <b>%s</b> table in database <b>%s</b> on <b>%s</b>?" % (table.name, table.db.name, table.db.host.name)):
				return
			if table.db.host.query("truncate `%s`" % (table.name)):
				table.refresh()
				self.redraw_table(table, iter)
				self.update_table_view()
		elif what == "drop_table":
			if not self.confirm("drop table", "do you really want to DROP the <b>%s</b> table in database <b>%s</b> on <b>%s</b>?" % (table.name, table.db.name, table.db.host.name)):
				return
			db = table.db
			if db.host.query("drop table `%s`" % (table.name)):
				new_tables = db.refresh()
				self.redraw_db(db, self.get_db_iter(db), new_tables)
				self.redraw_tables()
				
	def on_db_popup(self, item):
		path, column = self.connections_tv.get_cursor()
		iter = self.connections_model.get_iter(path)
		what = item.name
		db = self.connections_model.get_value(iter, 0)
		
		if what == "refresh_database":
			new_tables = db.refresh()
			self.redraw_db(db, iter, new_tables)
			self.redraw_tables()
		elif what == "drop_database":
			if not self.confirm("drop database", "do you really want to drop the <b>%s</b> database on <b>%s</b>?" % (db.name, db.host.name)):
				return
			host = db.host
			if host.query("drop database`%s`" % (db.name)):
				host.refresh()
				self.redraw_host(host, self.get_host_iter(host))
		elif what == "new_table":
			name = self.input("new table", "please enter the name of the new table:")
			if not name: return
			if db.host.query("create table `%s` (`%s_id` int primary key auto_increment)" % (name, name)):
				new_tables = db.refresh()
				self.redraw_db(db, self.get_db_iter(db), new_tables)
				self.redraw_tables()
	
	def on_host_popup(self, item):
		path, column = self.connections_tv.get_cursor()
		if path:
			iter = self.connections_model.get_iter(path)
			host = self.connections_model.get_value(iter, 0)
		else:
			iter = None
			host = None
		what = item.name
		
		if "connection_window" not in self.__dict__:
			self.connection_window = self.xml.get_widget("connection_window")
			self.xml.get_widget("cw_apply_button").connect("clicked", self.on_cw_apply)
			self.xml.get_widget("cw_test_button").connect("clicked", self.on_cw_test)
			self.xml.get_widget("cw_abort_button").connect("clicked", lambda *a: self.connection_window.hide())
			self.cw_props = ["name", "host", "port", "user", "password", "database"]
		
		if what == "refresh_host":
			host.refresh()
			self.redraw_host(host, iter)
		elif what == "new_database":
			name = self.input("new database", "please enter the name of the new database:")
			if not name: return
			if host.query("create database `%s`" % name):
				host.refresh()
				self.redraw_host(host, iter)
		elif what == "modify_connection":
			for n in self.cw_props:
				self.xml.get_widget("cw_%s" % n).set_text(host.__dict__[n])
			self.cw_mode = "edit"
			self.cw_host = host
			self.connection_window.show()
		elif what == "delete_connection":
			if not self.confirm("delete host", "do you really want to drop the host <b>%s</b>?" % (host.name)):
				return
			host.close()
			self.connections_model.remove(iter)
			if self.current_host == host:
				self.current_host = None
			del self.config["connection_%s" % host.name]
			host = None
			self.save_config()
		elif what == "new_connection":
			for n in self.cw_props:
				self.xml.get_widget("cw_%s" % n).set_text("")
			self.cw_mode = "new"
			self.connection_window.show()
		
	def on_cw_apply(self, *args):
		if self.cw_mode == "new":
			data = []
			for n in self.cw_props:
				data.append(self.xml.get_widget("cw_%s" % n).get_text())
			if not data[0]:
				self.connection_window.hide()
				return
			data.append(False)
			self.add_mysql_host(*data)
		else:
			for n in self.cw_props:
				self.cw_host.__dict__[n] = self.xml.get_widget("cw_%s" % n).get_text()
		self.save_config()
		self.connection_window.hide()
		
	def on_cw_test(self, *args):
		import _mysql;
		data = []
		for n in ["host", "user", "password"]:
			data.append(self.xml.get_widget("cw_%s" % n).get_text())
			
		try:
			handle = _mysql.connect(*data)
		except:
			self.show_message("test connection", "could not connect to host <b>%s</b> with user <b>%s</b> and password <b>%s</b>:\n<i>%s</i>" % (data[0], data[1], data[2], sys.exc_value[1]))
			return
		self.show_message("test connection", "successfully connected to host <b>%s</b> with user <b>%s</b>!" % (data[0], data[1]))
		handle.close()

	def get_db_iter(self, db):
		return self.get_connections_object_at_depth(db, 1)
		
	def get_host_iter(self, host):
		return self.get_connections_object_at_depth(host, 0)
		
	def get_connections_object_at_depth(self, obj, depth):
		d = 0
		model = self.connections_model
		iter = model.get_iter_first()
		while iter:
			if d == depth and model.get_value(iter, 0) == obj:
				return iter
			if d < depth and model.iter_has_child(iter):
				iter = model.iter_children(iter)
				d += 1
				continue
			new_iter = model.iter_next(iter)
			if not new_iter:
				iter = model.iter_parent(iter)
				d -= 1
				iter = model.iter_next(iter)
			else:
				iter = new_iter
		return None
		
	def on_query_popup(self, item):
		q = self.current_query
		path, column = q.treeview.get_cursor()
		iter = q.model.get_iter(path)
		
		if item.name == "copy_field_value":
			col_max = q.model.get_n_columns()
			for col_num in range(col_max):
				if column == q.treeview.get_column(col_num):
					break
			else:
				print "column not found!"
				return
			value = q.model.get_value(iter, col_num)
			self.clipboard.set_text(value)
			self.pri_clipboard.set_text(value)
		elif item.name == "copy_record_as_csv":
			col_max = q.model.get_n_columns()
			value = ""
			for col_num in range(col_max):
				if value: value += self.config["copy_record_as_csv_delim"]
				v = q.model.get_value(iter, col_num)
				if not v is None: value += v
			self.clipboard.set_text(value)
			self.pri_clipboard.set_text(value)
		elif item.name == "copy_column_as_csv":
			col_max = q.model.get_n_columns()
			for col_num in range(col_max):
				if column == q.treeview.get_column(col_num):
					break
			else:
				print "column not found!"
				return
			value = ""
			iter = q.model.get_iter_first()
			while iter:
				if value: value += self.config["copy_record_as_csv_delim"]
				v = q.model.get_value(iter, col_num)
				if not v is None: value += v
				iter = q.model.iter_next(iter)
			self.clipboard.set_text(value)
			self.pri_clipboard.set_text(value)
		elif item.name == "copy_column_names":
			value = ""
			for col in q.treeview.get_columns():
				if value: value += self.config["copy_record_as_csv_delim"]
				value += col.get_title().replace("__", "_")
			self.clipboard.set_text(value)
			self.pri_clipboard.set_text(value)
		elif item.name == "edit_field_value":
			col_max = q.model.get_n_columns()
			for col_num in range(col_max):
				if column == q.treeview.get_column(col_num):
					break
			else:
				print "column not found!"
				return
			table, where, field, value, row_iter = self.get_unique_where(q.last_source, path, col_num)
			self.field_edit.set_title("edit field %s in table %s where %s" % (field, table, where))
			b = self.field_edit_content.get_buffer()
			b.set_text(value)
			answer = self.field_edit.run()
			self.field_edit.hide();
			if answer != gtk.RESPONSE_OK:
				return
			new_value = b.get_text(b.get_start_iter(), b.get_end_iter())
			if new_value == value:
				return
			update_query = "update `%s` set `%s`='%s' where %s limit 1" % (table, field, self.current_host.escape(new_value), where)
			if self.current_host.query(update_query):
				q.model.set_value(row_iter, col_num, new_value)
		elif item.name == "set_value_null":
			col_max = q.model.get_n_columns()
			for col_num in range(col_max):
				if column == q.treeview.get_column(col_num):
					break
			else:
				print "column not found!"
				return
			table, where, field, value, row_iter = self.get_unique_where(q.last_source, path, col_num)
			update_query = "update `%s` set `%s`=NULL where %s limit 1" % (table, field, where)
			if self.current_host.query(update_query):
				q.model.set_value(row_iter, col_num, None)
		elif item.name == "set_value_now":
			col_max = q.model.get_n_columns()
			for col_num in range(col_max):
				if column == q.treeview.get_column(col_num):
					break
			else:
				print "column not found!"
				return
			table, where, field, value, row_iter = self.get_unique_where(q.last_source, path, col_num)
			update_query = "update `%s` set `%s`=now() where %s limit 1" % (table, field, where)
			if not self.current_host.query(update_query):
				return
			self.current_host.query("select `%s` from `%s` where %s limit 1" % (field, table, where))
			result = self.current_host.handle.store_result().fetch_row(0)
			if len(result) < 1:
				print "error: can't find modfied row!?"
				return
			q.model.set_value(row_iter, col_num, result[0][0])
		elif item.name == "set_value_unix_timestamp":
			col_max = q.model.get_n_columns()
			for col_num in range(col_max):
				if column == q.treeview.get_column(col_num):
					break
			else:
				print "column not found!"
				return
			table, where, field, value, row_iter = self.get_unique_where(q.last_source, path, col_num)
			update_query = "update `%s` set `%s`=unix_timestamp(now()) where %s limit 1" % (table, field, where)
			if not self.current_host.query(update_query):
				return
			self.current_host.query("select `%s` from `%s` where %s limit 1" % (field, table, where))
			result = self.current_host.handle.store_result().fetch_row(0)
			if len(result) < 1:
				print "error: can't find modfied row!?"
				return
			q.model.set_value(row_iter, col_num, result[0][0])
		elif item.name == "set_value_as_password":
			col_max = q.model.get_n_columns()
			for col_num in range(col_max):
				if column == q.treeview.get_column(col_num):
					break
			else:
				print "column not found!"
				return
			table, where, field, value, row_iter = self.get_unique_where(q.last_source, path, col_num)
			update_query = "update `%s` set `%s`=password('%s') where %s limit 1" % (table, field, self.current_host.escape(value), where)
			if not self.current_host.query(update_query):
				return
			self.current_host.query("select `%s` from `%s` where %s limit 1" % (field, table, where))
			result = self.current_host.handle.store_result().fetch_row(0)
			if len(result) < 1:
				print "error: can't find modfied row!?"
				return
			q.model.set_value(row_iter, col_num, result[0][0])
		elif item.name == "set_value_to_sha":
			col_max = q.model.get_n_columns()
			for col_num in range(col_max):
				if column == q.treeview.get_column(col_num):
					break
			else:
				print "column not found!"
				return
			table, where, field, value, row_iter = self.get_unique_where(q.last_source, path, col_num)
			update_query = "update `%s` set `%s`=sha1('%s') where %s limit 1" % (table, field, self.current_host.escape(value), where)
			if not self.current_host.query(update_query):
				return
			self.current_host.query("select `%s` from `%s` where %s limit 1" % (field, table, where))
			result = self.current_host.handle.store_result().fetch_row(0)
			if len(result) < 1:
				print "error: can't find modfied row!?"
				return
			q.model.set_value(row_iter, col_num, result[0][0])
		
	def on_template(self, button, t):
		current_table = self.get_selected_table()
		current_fc_table = current_table;
		
		if t.find("$table$") != -1:
			if not current_table:
				show_message("info", "no table selected!\nyou can't execute a template with $table$ in it, if you have no table selected!")
				return
			t = t.replace("$table$", self.escape_fieldname(current_table.name))
			
		pos = t.find("$primary_key$")
		if pos != -1:
			if not current_table:
				show_message("info", "no table selected!\nyou can't execute a template with $primary_key$ in it, if you have no table selected!")
				return
			if not current_table.fields:
				show_message("info", "sorry, can't execute this template, because table '%s' has no fields!" % current_table.name)
				return
			# is the next token desc or asc?
			result = re.search("(?i)[ \t\r\n]*(de|a)sc", t[pos:])
			order_dir = ""
			if result: 
				o = result.group(1).lower()
				if o == "a":
					order_dir = "asc"
				else:
					order_dir = "desc"
			
			replace = ""
			while 1:
				primary_key = ""
				for name, props in current_table.fields.iteritems():
					if props[3] != "PRI": continue
					if primary_key: primary_key += " " + order_dir + ", "
					primary_key += self.escape_fieldname(name)
				if primary_key: 
					replace = primary_key
					break
				key = ""
				for name, props in current_table.fields.iteritems():
					if props[3] != "UNI": continue
					if key: key +=  " " + order_dir + ", "
					key += self.escape_fieldname(name)
				if key: 
					replace = key
					break
				replace = self.escape_fieldname(current_table.field_order[0])
				break
			t = t.replace("$primary_key$", replace)
			
		if t.find("$field_conditions$") != -1:
			if not self.field_conditions_initialized:
				self.field_conditions_initialized = True
				self.fc_count = 4
				self.fc_window = self.xml.get_widget("field_conditions")
				table = self.xml.get_widget("fc_table")
				table.resize(1 + self.fc_count, 4)
				self.fc_entry = []
				self.fc_combobox = []
				self.fc_op_combobox = []
				self.fc_logic_combobox = []
				for i in range(self.fc_count):
					self.fc_entry.append(gtk.Entry())
					self.fc_entry[i].connect("activate", lambda *e: self.fc_window.response(gtk.RESPONSE_OK))
					self.fc_combobox.append(gtk.combo_box_new_text())
					self.fc_op_combobox.append(gtk.combo_box_new_text())
					self.fc_op_combobox[i].append_text("=")
					self.fc_op_combobox[i].append_text("<")
					self.fc_op_combobox[i].append_text(">")
					self.fc_op_combobox[i].append_text("!=")
					self.fc_op_combobox[i].append_text("LIKE")
					if i:
						self.fc_logic_combobox.append(gtk.combo_box_new_text())
						self.fc_logic_combobox[i - 1].append_text("disabled")
						self.fc_logic_combobox[i - 1].append_text("AND")
						self.fc_logic_combobox[i - 1].append_text("OR")
						table.attach(self.fc_logic_combobox[i - 1], 0, 1, i + 1, i + 2)
						self.fc_logic_combobox[i - 1].show()
					table.attach(self.fc_combobox[i], 1, 2, i + 1, i + 2);
					table.attach(self.fc_op_combobox[i], 2, 3, i + 1, i + 2)
					table.attach(self.fc_entry[i], 3, 4, i + 1, i + 2);
					self.fc_combobox[i].show();
					self.fc_op_combobox[i].show();
					self.fc_entry[i].show();
			if not current_table:
				show_message("info", "no table selected!\nyou can't execute a template with $field_conditions$ in it, if you have no table selected!")
				return
				
			last_field = []
			for i in range(self.fc_count):
				last_field.append(self.fc_combobox[i].get_active_text())
				self.fc_combobox[i].get_model().clear()
				if i:
					self.fc_logic_combobox[i - 1].set_active(0)
			fc = 0
			for field_name in current_table.field_order:
				for k in range(self.fc_count):
					self.fc_combobox[k].append_text(field_name)
					if last_field[k] == field_name:
						self.fc_combobox[k].set_active(fc)
				fc += 1
			if not self.fc_op_combobox[0].get_active_text(): self.fc_op_combobox[0].set_active(0)
			if not self.fc_combobox[0].get_active_text(): self.fc_combobox[0].set_active(0)
			
			answer = self.fc_window.run()
			self.fc_window.hide()
			if answer != gtk.RESPONSE_OK: return
			
			conditions = "`%s` %s '%s'" % (
				self.fc_combobox[0].get_active_text(),
				self.fc_op_combobox[0].get_active_text(),
				self.current_host.escape(self.fc_entry[0].get_text())
			)
			for i in range(1, self.fc_count):
				if self.fc_logic_combobox[i - 1].get_active_text() == "disabled" or self.fc_combobox[i].get_active_text() == "" or self.fc_op_combobox[i].get_active_text() == "":
					continue
				conditions += " %s `%s` %s '%s'" % (
					self.fc_logic_combobox[i - 1].get_active_text(),
					self.fc_combobox[i].get_active_text(),
					self.fc_op_combobox[i].get_active_text(),
					self.current_host.escape(self.fc_entry[i].get_text())
				)
			t = t.replace("$field_conditions$", conditions)
		self.on_execute_query_clicked(None, t)
		
	def on_processlist_refresh_timeout(self, button):
		value = button.get_value()
		if value < 0.1:
			self.processlist_timer_running = False
			return False
		self.refresh_processlist()
		if value != self.processlist_timer_interval:
			self.processlist_timer_running = False
			self.on_processlist_refresh_value_change(button)
			return False
		return True

	def on_processlist_button_release(self, tv, event):
		if not event.button == 3: return False
		res = tv.get_path_at_pos(int(event.x), int(event.y));
		if not res: return False
		self.xml.get_widget("processlist_popup").popup(None, None, None, event.button, event.time)
		
	def show_message(self, title, message):
		dialog = gtk.MessageDialog(self.mainwindow, gtk.DIALOG_MODAL, gtk.MESSAGE_INFO, gtk.BUTTONS_OK, message)
		dialog.label.set_property("use-markup", True)
		dialog.set_title(title)
		dialog.run()
		dialog.hide()
		
	def confirm(self, title, message):
		dialog = gtk.MessageDialog(self.mainwindow, gtk.DIALOG_MODAL, gtk.MESSAGE_QUESTION, gtk.BUTTONS_YES_NO, message)
		dialog.label.set_property("use-markup", True)
		dialog.set_title(title)
		answer = dialog.run()
		dialog.hide()
		return answer == gtk.RESPONSE_YES
		
	def input(self, title, message, default = ""):
		dialog = gtk.Dialog(title, self.mainwindow, gtk.DIALOG_MODAL, 
			(gtk.STOCK_OK, gtk.RESPONSE_ACCEPT,
			 gtk.STOCK_CANCEL, gtk.RESPONSE_REJECT))
		label = gtk.Label(message)
		label.set_property("use-markup", True)
		dialog.vbox.pack_start(label, True, True, 2)
		entry = gtk.Entry()
		entry.connect("activate", lambda *a: dialog.response(gtk.RESPONSE_ACCEPT))
		dialog.vbox.pack_start(entry, False, True, 2)
		label.show()
		entry.show()
		entry.set_text(default)
		answer = dialog.run()
		dialog.hide()
		if answer != gtk.RESPONSE_ACCEPT:
			return None
		return entry.get_text()

	def render_connections_pixbuf(self, column, cell, model, iter):
		d = model.iter_depth(iter)
		o = model.get_value(iter, 0)
		if d == 0:
			if o.connected:
				cell.set_property("pixbuf", self.icons["host"])
			else:
				cell.set_property("pixbuf", self.icons["offline_host"])
		elif d == 1:
			cell.set_property("pixbuf", self.icons["db"])
		elif d == 2:
			cell.set_property("pixbuf", self.icons["table"])
		elif d == 3:
			cell.set_property("pixbuf", self.icons["field"])
		else:
			print "unknown depth", d," for render_connections_pixbuf with object", o
		
	def render_connections_text(self, column, cell, model, iter):
		d = model.iter_depth(iter)
		o = model.get_value(iter, 0)
		if d == 0:
			if o.connected:
				cell.set_property("text", o.name)
			else:
				cell.set_property("text", "(%s)" % o.name)
		elif d == 3: #fields are only strings
			cell.set_property("text", "%s %s" % (o[0], o[1]))
		else: # everything else has a name
			cell.set_property("text", o.name)
			#print "unknown depth", d," for render_connections_pixbuf with object", o
			
	def render_mysql_string(self, column, cell, model, iter, id):
		o = model.get_value(iter, id)
		if not o is None: 
			cell.set_property("background", None)
			cell.set_property("text", o)
		else:
			cell.set_property("background", self.config["null_color"])
			cell.set_property("text", "")
		
	def config_get_bool(self, name):
		value = self.config[name].lower()
		if value == "yes": return True
		if value == "y": return True
		if value == "1": return True
		if value == "true": return True
		if value == "t": return True
		return False
		
	def get_config_file_name(self):
		for i in ["HOME", "USERPROFILE"]: 
			filename = os.getenv(i)
			if filename: break
		if not filename:
			filename = "."
		return filename + "/.emma"
		
	def save_config(self):
		filename = self.get_config_file_name()
		try:
			fp = file(filename, "w")
		except:
			self.show_message("save config file", "could not open %s for writing: %s" % (filename, sys.exc_value))
			return
			
		keys = self.config.keys()
		keys.sort()
		for name in keys:
			if name.startswith("connection_"):
				continue
			value = self.config[name]
			fp.write("%s=%s\n" % (name, value))
			
		iter = self.connections_model.get_iter_root()
		while iter:
			host = self.connections_model.get_value(iter, 0)
			fp.write("connection_%s=%s\n" % (host.name, host.get_connection_string()))
			iter = self.connections_model.iter_next(iter)
		fp.close()
		
	def load_config(self, unpickled = False):
		filename = self.get_config_file_name()
		# todo get_charset(self.config["db_codeset"]);
		# printf("system charset: '%s'\n", self.config["db_codeset"].c_str());
		# syntax_highlight_functions: grep -E -e "^[ \\t]+<code class=\"literal[^>]*>[^\(<90-9]+\(" mysql_fun.html fun*.html | sed -r -e "s/^[^<]*<code[^>]+>//" -e "s/\(.*$/,/" | tr "[:upper:]" "[:lower:]" | sort | uniq | xargs echo
		self.config = {
			"null_color": "#00eeaa",
			"autorefresh_interval_table": "300",
			"column_sort_use_newline": "true",
			"query_text_font": "Monospace 8",
			"query_text_wrap": "false",
			"query_result_font": "Monospace 8",
			"query_log_max_entry_length": "1024",
			"result_view_column_width_min": "70",
			"result_view_column_width_max": "300",
			"result_view_column_resizable": "false",
			"result_view_column_sort_timeout": "750",
			"syntax_highlight_keywords": "lock, unlock, tables, kill, truncate table, alter table, host, database, field, comment, show table status, show index, add index, drop index, add primary key, add unique, drop primary key, show create table, values, insert into, into, select, show databases, show tables, show processlist, show tables, from, where, order by, group by, limit, left, join, right, inner, after, alter, as, asc, before, begin, case, column, change column, commit, create table, default, delete, desc, describe, distinct, drop, table, first, grant, having, insert, interval, insert into, limit, null, order, primary key, primary, auto_increment, rollback, set, start, temporary, union, unique, update, create database, use, key, type, uniqe key, on, type, not, unsigned",
			"syntax_highlight_functions": "date_format, now, floor, rand, hour, if, minute, month, right, year, isnull",
			"syntax_highlight_functions": "abs, acos, adddate, addtime, aes_decrypt, aes_encrypt, ascii, asin, atan, benchmark, bin, bit_length, ceil, ceiling, char, character_length, char_length, charset, coercibility, collation, compress, concat, concat_ws, connection_id, conv, convert_tz, cos, cot, crypt, curdate, current_date, current_time, current_timestamp, current_user, curtime, database, date, date_add, datediff, date_format, date_sub, day, dayname, dayofmonth, dayofweek, dayofyear, decode, default, degrees, des_decrypt, des_encrypt, elt, encode, encrypt, exp, export_set, extract, field, find_in_set, floor, format, found_rows, from_days, from_unixtime, get_format, get_lock, hex, hour, if, ifnull, inet_aton, inet_ntoa, insert, instr, is_free_lock, is_used_lock, last_day, last_insert_id, lcase, left, length, ln, load_file, localtime, localtimestamp, locate, log, lower, lpad, ltrim, makedate, make_set, maketime, master_pos_wait, microsecond, mid, minute, mod, month, monthname, mysql_insert_id, now, nullif, oct, octet_length, old_password, ord, order by rand, password, period_add, period_diff, pi, position, pow, power, quarter, quote, radians, rand, release_lock, repeat, replace, reverse, right, round, row_count, rpad, rtrim, schema, second, sec_to_time, session_user, sha, sign, sin, sleep, soundex, space, sqrt, str_to_date, subdate, substr, substring, substring_index, subtime, sysdate, system_user, tan, time, timediff, time_format, timestamp, timestampadd, timestampdiff, time_to_sec, to_days, trim, truncate, ucase, uncompress, uncompressed_length, unhex, unix_timestamp, upper, user, utc_date, utc_time, utc_timestamp, uuid, version, week, weekday, weekofyear, year, yearweek",
			"syntax_highlight_datatypes": "binary, bit, blob, boolean, char, character, dec, decimal, double, float, int, integer, numeric, smallint, timestamp, varchar, datetime, text, mediumint, bigint, tinyint, date",
			"syntax_highlight_operators": "not, and, or, like, \\<, \\>",
			"syntax_highlight_fg_keyword": "#00007F",
			"syntax_highlight_fg_function": "darkblue",
			"syntax_highlight_fg_datatype": "#AA00AA",
			"syntax_highlight_fg_operator": "#0000aa",
			"syntax_highlight_fg_double-quoted-string": "#7F007F",
			"syntax_highlight_fg_single-quoted-string": "#9F007F",
			"syntax_highlight_fg_backtick-quoted-string": "#BF007F",
			"syntax_highlight_fg_number": "#007F7F",
			"syntax_highlight_fg_comment": "#007F00",
			"syntax_highlight_fg_error": "red",
			"pretty_print_uppercase_keywords": "false",
			"pretty_print_uppercase_operators": "false",
			"template1_last 150 records": "select * from $table$ order by $primary_key$ desc limit 150",
			"template2_500 records in fs-order": "select * from $table$ limit 500",
			"template3_quick filter 500": "select * from $table$ where $field_conditions$ limit 500",
			"copy_record_as_csv_delim": ",",
			"save_result_as_csv_delim": ",",
			"save_result_as_csv_line_delim": "\\n",
			"ping_connection_interval": "300"
		}
		first = False
		try:
			fp = file(filename, "r")
			line_no = 0
			for line in fp:
				line_no += 1
				line.lstrip(" \t\r\n")
				if not line: continue
				if line[0] == '#': continue
				varval = line.split("=", 1)
				if len(varval) != 2:
					print "skipping invalid self.config line %d: '%s'" % (line_no, line)
					continue
				self.config[varval[0].strip("\r\n \t")] = varval[1].strip("\r\n \t")
			fp.close()
		except:
			print "got exception:", sys.exc_type, sys.exc_value
			first = True
			self.config["connection_localhost"] = "localhost,localhost,root,,"

		self.first_template = None
		keys = self.config.keys()
		keys.sort()
		for name in keys:
			value = self.config[name]
			if not unpickled:
				prefix = "connection_"
				if name.startswith(prefix):
					v = value.split(",")
					port = ""
					p = v[0].rsplit(":", 1)
					if len(p) == 2:
						port = p[1]
						v[0] = p[0]
					self.add_mysql_host(name[len(prefix):], v[0], port, v[1], v[2], v[3], first)
			
			prefix = "template";
			if name.startswith(prefix):
				value = value.replace("`$primary_key$`", "$primary_key$")
				value = value.replace("`$table$`", "$table$")
				value = value.replace("`$field_conditions$`", "$field_conditions$")
				self.config[name] = value
				if not self.first_template:
					self.first_template = value
				p = name.split("_", 1)
				button = gtk.ToolButton(gtk.STOCK_EXECUTE)
				button.set_tooltip(self.tooltips, "%s\n%s" % (p[1], value))
				button.connect("clicked", self.on_template, value)
				self.xml.get_widget("query_toolbar").insert(button, -1)
				button.show()
	
		if not unpickled: return
		for h in self.hosts:
			h.__init__(self.add_sql_log, self.add_msg_log)
			iter = self.connections_model.append(None, [h])
			h.set_update_ui(self.redraw_host, iter) # call before init!
			self.redraw_host(h, iter)
			self.current_host = h
		
	def add_mysql_host(self, name, hostname, port, user, password, database, isfirst):
		host = mysql_host(self.add_sql_log, self.add_msg_log, name, hostname, port, user, password, database)
		iter = self.connections_model.append(None, [host])
		host.set_update_ui(self.redraw_host, iter)
	
	def add_sql_log(self, log):
		max_len = int(self.config["query_log_max_entry_length"])
		if len(log) > max_len:
			log = log[0:max_len] + "\n/* query with length of %d bytes truncated. */" % len(log);
		
		# query = db_to_utf8(log);
		# query = syntax_highlight_markup(query);
		# query = rxx.replace(query, "[\r\n\t ]+", " ", Regexx::global);
		if not log: return
			
		now = time.time()
		now = int((now - int(now)) * 100)
		timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
		if now: timestamp = "%s.%02d" % (timestamp, now)
		iter = self.sql_log_model.append((timestamp, log))
		self.sql_log_tv.scroll_to_cell(self.sql_log_model.get_path(iter))
		#self.xml.get_widget("message_notebook").set_current_page(0)
		self.process_events()
		
	def process_events(self):
 		while gtk.events_pending():
			gtk.main_iteration(False)

	def add_msg_log(self, log):
		if not log: return
		
		log.replace(
			"You have an error in your SQL syntax.  Check the manual that corresponds to your MySQL server version for the right syntax to use near",
			"syntax error at "
		)
		now = time.time()
		now = int((now - int(now)) * 100)
		timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
		if now: timestamp = "%s.%02d" % (timestamp, now)
		iter = self.msg_model.append((timestamp, log))
		self.msg_tv.scroll_to_cell(self.msg_model.get_path(iter))
		self.xml.get_widget("message_notebook").set_current_page(1)

	def get_selected_table(self):
		path, column = self.connections_tv.get_cursor()
		depth = len(path)
		iter = self.connections_model.get_iter(path)
		if depth == 3:
			return self.connections_model.get_value(iter, 0)
		return None

	def load_icons(self):
		self.icons = {}
		for icon in ["offline_host", "host", "db", "table", "field", "emma"]:
			filename = os.path.join(icons_path, icon + ".png")
			try: 
				self.icons[icon] = gtk.gdk.pixbuf_new_from_file(filename)
			except: 
				print "could not load", filename
		self.mainwindow.set_icon(self.icons["emma"])
		
	def refresh_processlist(self, *args):
		if not self.current_host: return
		self.current_host.refresh_processlist()
		self.redraw_processlist(self.current_host)
		
	def redraw_processlist(self, host):
		if not host.processlist: return
		fields, rows = host.processlist
		if self.processlist_model: self.processlist_model.clear()
			
		if self.current_processlist_host != self.current_host:
			self.current_processlist_host = self.current_host
			self.xml.get_widget("version_label").set_text("  server version: %s" % self.current_host.handle.get_server_info());
			
			for col in self.processlist_tv.get_columns():
				self.processlist_tv.remove_column(col)
			
			columns = [gobject.TYPE_STRING] * len(fields)
			self.processlist_model = gtk.ListStore(*columns);
			self.processlist_tv.set_model(self.processlist_model);
			self.processlist_tv.set_headers_clickable(True);
			id = 0
			for field in fields:
				title = field[0].replace("_", "__")
				self.processlist_tv.insert_column_with_data_func(-1, title, gtk.CellRendererText(), self.render_mysql_string, id)
				id += 1
			
		for proc in rows:
			self.processlist_model.append(proc)
			
		return
	
	def redraw_tables(self):
		if not self.current_host:
			return
		db = self.current_host.current_db
		if not db:
			return
		if not "tables_tv" in self.__dict__:
			self.tables_tv = self.xml.get_widget("tables_treeview")
			self.tables_model = None
			self.tables_db = None
			
		if not self.tables_db == db:
			self.tables_db = db
			if self.tables_model: 
				self.tables_model.clear()
				for col in self.tables_tv.get_columns():
					self.tables_tv.remove_column(col)

			fields = db.status_headers
			columns = [gobject.TYPE_STRING] * len(fields)
			self.tables_model = gtk.ListStore(*columns);
			self.tables_tv.set_model(self.tables_model);
			id = 0
			for field in fields:
				title = field.replace("_", "__")
				self.tables_tv.insert_column_with_data_func(-1, title, gtk.CellRendererText(), self.render_mysql_string, id)
				id += 1
			self.tables_count = 0
		
		keys = db.tables.keys()
		if self.tables_count == len(keys): return
		self.tables_count = len(keys)
		keys.sort()
		self.tables_model.clear()
		for name in keys:
			table = db.tables[name]
			self.tables_model.append(table.props)
	
	def redraw_entry(self, obj, iter):
		print "do redraw", obj, iter
		
	def redraw_host(self, host, iter, expand = False):
		#print "redraw host", host.name
		if host.expanded: expand = True
			
		# first remove exiting children of that node
		i = self.connections_model.iter_children(iter)
		while i and self.connections_model.iter_is_valid(i):
			self.connections_model.remove(i)
		
		# now add every database
		keys = host.databases.keys()
		keys.sort()
		for name in keys:
			db = host.databases[name]
			i = self.connections_model.append(iter, (db,))
			if expand:
				self.connections_tv.expand_row(self.connections_model.get_path(iter), False)
				expand = False
			self.redraw_db(db, i)
			
	def redraw_db(self, db, iter, new_tables = None, force_expand = False):
		#print "redraw db", db.name
		if not iter:
			print "Error: invalid db-iterator:", iter
			return
		path = self.connections_model.get_path(iter)
		if db.expanded: 
			force_expand = True
		i = self.connections_model.iter_children(iter)
		while i and self.connections_model.iter_is_valid(i): 
			self.connections_model.remove(i)
		keys = db.tables.keys()
		keys.sort()
		iterators = {}
		for name in keys:
			table = db.tables[name]
			i = self.connections_model.append(iter, (table,))
			if force_expand: 
				self.connections_tv.expand_row(path, False)
				force_expand = False
			self.redraw_table(table, i)
			iterators[name] = i
		if not new_tables: return
		for name in new_tables:
			table = db.tables[name]
			table.refresh(False)
			self.redraw_table(table, iterators[name])
			self.process_events()
			
	def redraw_table(self, table, iter):
		#print "redraw table", table.name
		if table.expanded: self.connections_tv.expand_row(self.connections_model.get_path(iter), False)
		i = self.connections_model.iter_children(iter)
		while i and self.connections_model.iter_is_valid(i): 
			self.connections_model.remove(i)
		for field in table.field_order:
			i = self.connections_model.append(iter, (table.fields[field],))
		
def start():
	global new_instance
	e = Emma()
	while 1:
		print "running", e
		gtk.main()
		del e
		if not new_instance: break
		e = new_instance
		new_instance = None
		print "initializing", e
		e.__init__()

if __name__ == "__main__": start()
	