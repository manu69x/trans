import os
from lxml import etree
SCHEMAS = os.path.dirname(os.path.abspath(__file__))
dtd = etree.DTD(os.path.join(SCHEMAS, "tmx14.dtd"))
print("TMX 1.4 DTD loaded")
tmx = """<?xml version="1.0" encoding="UTF-8"?>
<tmx version="1.4" createid="trans" createagency="trans" createtool="trans"
     createdate="20260908T18:00:00Z" xmlns="http://www.lisa.org/tmx14">
  <header o-tms="Trans TMS" adminlang="en" srclang="en">
    <file-type product="trans" name="Test book" creationid="trans"
               creationdate="20260908T18:00:00Z" tby="Trans"
               srclang="en" segtype="paragraph"/>
  </header>
  <body>
    <tu tuid="seg-1">
      <tuv xml:lang="en"><seg>Chapter one, line 1.</seg></tuv>
      <tuv xml:lang="it"><seg>Capitolo uno, riga 1.</seg></tuv>
    </tu>
    <tu tuid="seg-2">
      <tuv xml:lang="en"><seg>Chapter one, line 2.</seg></tuv>
      <tuv xml:lang="it"><seg>Capitolo uno, riga 2.</seg></tuv>
    </tu>
  </body>
</tmx>
"""
troot = etree.fromstring(tmx.encode("utf-8"))
print("TMX 1.4 valid against DTD:", dtd.validate(troot))
if not dtd.validate(troot):
    print("ERRORS:\n", dtd.error_log)
