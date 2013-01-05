#!/usr/bin/env python

import sys
import logging
import cgi
import traceback
from google.appengine.ext import webapp
from google.appengine.ext.webapp import util
from google.appengine.api import users
from google.appengine.api import memcache
from google.appengine.api.urlfetch import DownloadError
import gdata.spreadsheet.text_db
import gdata.alt.appengine

from mako.template import Template
from mako.lookup import TemplateLookup
from mako.runtime import Context
from StringIO import StringIO

token = None
#token = 'insert token here and uncomment'

website_domain = 'example.com'

debug = False

spreadsheet = 'spreadcms'
table_pages = 'pages'
table_pages_beta = 'pages_beta' # for testing
table_templates = 'templates' # table where templates are stored
col_name = 'name'
col_content = 'content'

# global to use caching
database_connection = None
no_of_templates = None

class DatabaseConnection:
  """ connection to the spreadsheet """
  def __init__(self):
    logging.debug("#### init db")
    # use stored token
    self.client = gdata.spreadsheet.text_db.DatabaseClient()
    self.client._GetDocsClient().SetAuthSubToken(token)
    self.client._GetSpreadsheetsClient().SetAuthSubToken(token)   
    # set deadline
    gdata.alt.appengine.run_on_appengine(self.client, deadline=10)
    gdata.alt.appengine.run_on_appengine(self.client._GetDocsClient(), deadline=10)
    gdata.alt.appengine.run_on_appengine(self.client._GetSpreadsheetsClient(), deadline=10)
    
    self.db = self.client.GetDatabases(name=spreadsheet)[0]
    self.tables = []
    for table in self.db.GetTables():
      name = table.entry.title.text
      self.tables.append(name)

class Database:
  """ wrapper for the spreadsheet. places the tables into its __dict__ """
  
  def __init__(self, database_connection, use_cache=True):  
    self.database_connection = database_connection
    self.use_cache=use_cache
    for table in database_connection.tables:
      self.__dict__[table] = Table(self, table)
        
  def getTableRecord(self, table, row_no):
    key = "tbl_%s_%s" % (table, row_no)
    data = None
    if self.use_cache:
      data = memcache.get(key)
    if data is None:
      logging.debug("#### database: get " + key)    
      record = self.database_connection.db.GetTables(name=table)[0].GetRecord(row_number=row_no)
      data = record.content
      for item, value in data.items():
        if value=="#N/A":
          logging.error("#### database: retrieved #N/A for " + key)    
          raise DownloadError
      if self.use_cache:
        memcache.add(key, data)
    return data

class Table: 
  """ wraps a spreadsheet table. access rows with their index (starting with 1) """
  def __init__(self, database, table):  
    self.database = database
    self.table = table
    
  def __getitem__(self, index):
    return self.database.getTableRecord(self.table, index)
      
    

class MainHandler(webapp.RequestHandler):
  """ the main web request handler """
  def connect(self):
  
    if (token is None) :
      # handling the case we do not have a token to access the spreadsheet yet      
      token_param = self.request.get('token')
      client = gdata.spreadsheet.text_db.DatabaseClient()
      
      if len(token_param)>0:
        # Upgrade the single-use AuthSub token to a multi-use session token.
        auth_token = gdata.auth.extract_auth_sub_token_from_url(self.request.uri)        
        client._GetDocsClient().SetAuthSubToken(auth_token)
        client._GetDocsClient().UpgradeToSessionToken(auth_token)
        session_token = client._GetDocsClient().GetAuthSubToken()        
        self.response.out.write("The tokens is: <pre>token = '%s'</pre>" % ( session_token ) )                   
      else:
        # redirect to authentication url
        next_url = self.request.url
        auth_url = client._GetDocsClient().GenerateAuthSubURL(
          next_url,scope='http://spreadsheets.google.com/feeds/ http://docs.google.com/feeds/documents/', 
          secure=False, session=True, domain=website_domain)
        self.redirect(str(auth_url))
              
      return False

    try:
      global database_connection
      if (database_connection is None):
        database_connection = DatabaseConnection()
    except gdata.spreadsheet.text_db.Error, err:
      self.response.out.write(err)
      return False
      
    return True      
  
  
  def retry(self):
    retries = self.request.get('r')
    if retries is None:
      retries = 1
    else:
      try:
        retries = int(retries)+1
      except:
        retries = 1
    if retries < 4:
      self.redirect(self.request.path + "?r=" + str(retries))
    else:
      self.response.out.write("Sorry. Could not connect to database. Please try again later.")                   
          
          

  def process(self, table_for_pages=table_pages, use_cache=True):        

    database = Database(database_connection, use_cache=use_cache)
    
    # get page id, default to 1
    pageid = 1    
    parts = self.request.path.strip('/').split('/')
    if len(parts)==2 and parts[0]=="page":
      try: pageid = int(parts[1]) 
      except ValueError: pass

    page = database.getTableRecord(table_for_pages, pageid)  # same as database.__dict__[table_for_pages][pageid]

    mylookup = TemplateLookup(filesystem_checks=False, input_encoding='utf-8', output_encoding='utf-8')
    
    #load all templates
    global no_of_templates
    i = 1
    while True:
      try:    
        template = database.getTableRecord(table_templates,i)
        logging.debug("### loading template " + template[col_name])
        mylookup.put_template(template[col_name], 
          Template(unicode(template[col_content]), lookup=mylookup, input_encoding='utf-8', output_encoding='utf-8'))
      except DownloadError:
        self.retry()
        return
      except:
        break
      i+=1
      if not no_of_templates is None:
        if i > no_of_templates:
          break
    no_of_templates = i-1
        
    content = page[col_content] or ""
    
    # invoke Mako
    try:
      t = Template(unicode(content), lookup=mylookup, input_encoding='utf-8', output_encoding='utf-8')
      mylookup.put_template("_page"+str(pageid), t)
      s = t.render(page=page, pageid=pageid, db=database)
      self.response.out.write(s)
    except Exception,e:  
      s = "<h1>Error</h1>"
      s += "<pre>" + cgi.escape(str(e)) + "</pre>"
      s += "<h1>Page</h1>"
      s += "<pre>" + cgi.escape(page[col_content]) + "</pre>"
      exc_type, exc_value, exc_traceback = sys.exc_info()
      s += "<h1>Call Stack</h1>"
      s += "<pre>" + "".join(traceback.format_tb(exc_traceback)) + "</pre>"

      logging.error(s)
      if debug:
        self.response.out.write(s)
      else:
        self.response.out.write("An error occurred.")
        
        
  def get(self):        
    try:
      if not self.connect():
        return
      if self.request.cookies.get('beta_mode', 'off') == 'on':
        self.process(table_for_pages=table_pages_beta, use_cache=False)
      else:
        self.process()
    except DownloadError:
      self.retry()          



    
class AdminHandler(webapp.RequestHandler):
  """ the admin request handler """
  def get(self):  
    beta = self.request.cookies.get('beta_mode', 'off')  
    out = """
    <html><body>
    <a href="/">home</a><br/>
    <a href="/admin/refresh">refresh</a><br/>
    Beta mode = %s <br/>
    <a href="/admin/beta/on">turn beta site mode on</a><br/>
    <a href="/admin/beta/off">turn beta site mode off</a><br/>
    </body></html>
    """ % (beta)
    self.response.out.write(out)
  
class RefreshHandler(webapp.RequestHandler):
  def get(self):  
    global database_connection  
    global no_of_templates    
    database_connection = None
    no_of_templates = None
    memcache.flush_all()
    self.response.out.write('flushed memcache<br/><a href="/admin/">back</a>')  
  
class BetaOnHandler(webapp.RequestHandler):
  def get(self):  
    self.response.headers.add_header('Set-Cookie', 'beta_mode=%s; path=/' % 'on')
    self.response.out.write('activated beta mode<br/><a href="/admin/">back</a>')  
    
class BetaOffHandler(webapp.RequestHandler):
  def get(self):  
    self.response.headers.add_header('Set-Cookie', 'beta_mode=%s; path=/' % 'off')
    self.response.out.write('turned off beta mode<br/><a href="/admin/">back</a>')  

  
def main():
  application = webapp.WSGIApplication([('/', MainHandler),
                                        ('/page/[0-9]+', MainHandler),
                                        ('/admin/', AdminHandler),
                                        ('/admin/refresh', RefreshHandler),
                                        ('/admin/beta/on', BetaOnHandler),
                                        ('/admin/beta/off', BetaOffHandler)],
                                       debug=debug)
  logging.getLogger().setLevel(logging.DEBUG if debug else logging.WARNING)
  util.run_wsgi_app(application)


if __name__ == '__main__':
  main()
