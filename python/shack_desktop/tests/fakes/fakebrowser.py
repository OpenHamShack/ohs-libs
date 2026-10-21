"""A stand-in default browser ($BROWSER for webbrowser.open): loads the
URL as a browser would, keeping the cookie, and logs what it got."""
import http.cookiejar
import json
import os
import sys
import urllib.error
import urllib.request

url = sys.argv[1]
jar = http.cookiejar.CookieJar()
opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
try:
    r = opener.open(url, timeout=5)
    status, final, body = r.status, r.geturl(), r.read().decode()
except urllib.error.HTTPError as e:
    status, final, body = e.code, url, ""
with open(os.environ["FAKE_BROWSER_LOG"], "a") as f:
    f.write(json.dumps({"call": "browser", "status": status, "final_url": final, "body": body}) + "\n")
