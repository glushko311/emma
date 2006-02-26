import sys
import traceback
from mysql_table import *

class mysql_db:
	def __init__(self, host, name):
		self.handle = host.handle
		self.host = host
		self.name = name
		self.id = id
		self.expanded = False
		self.status_headers = []
		self.tables = {}
		
	def refresh(self):
		self.host.select_database(self)
		if not self.host.query("show table status"): return
		new_tables = []
		
		result = self.handle.store_result()
		self.status_headers = []
		for h in result.describe():
			self.status_headers.append(h[0])
		old = dict(zip(self.tables.keys(), range(len(self.tables))))
		for row in result.fetch_row(0):
			if not row[0] in old:
				#print "new table", row[0]
				self.tables[row[0]] = mysql_table(self, row)
				new_tables.append(row[0])
			else:
				#print "known table", row[0]
				# todo update self.tables[row[0]] with row!
				del old[row[0]]
				
		for table in old:
			print "destroy table", table
			del self.tables[table]
		return new_tables