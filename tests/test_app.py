import io, os, time, zipfile
os.environ["PDF_SPLITTER_PIN"] = "1234"
os.environ["PDF_SPLITTER_DATA"] = os.path.join(os.environ.get("TMPDIR", "/tmp"), "pdfsplit-test")
os.environ["PDF_SPLITTER_TTL_SECONDS"] = "3"

from fastapi.testclient import TestClient
from pypdf import PdfReader, PdfWriter
import app.main as m


def make_pdf(n):
    w = PdfWriter()
    for i in range(n):
        w.add_blank_page(300 + i, 400)
    b = io.BytesIO(); w.write(b); return b.getvalue()


def up(c, pin="1234", data=None):
    return c.post("/api/upload", data={"username": "t", "pin": pin},
                  files={"file": ("My Doc.pdf", data or make_pdf(25), "application/pdf")})


def test_flow():
    with TestClient(m.app) as c:
        assert up(c, pin="bad").status_code == 403
        r = up(c); assert r.status_code == 200, r.text
        j = r.json(); assert j["pages"] == 25
        assert c.get(f"/api/jobs/{j['id']}/thumb/3").headers["content-type"] == "image/jpeg"
        r = c.post(f"/api/jobs/{j['id']}/split", json={"pages_per_file": 10, "rotations": {"0": 180, "1": 90}})
        z = r.json()["zips"]; assert len(z) == 1
        zf = zipfile.ZipFile(io.BytesIO(c.get(z[0]["url"]).content))
        names = sorted(zf.namelist()); assert len(names) == 3, names
        first = PdfReader(io.BytesIO(zf.read(names[0])))
        assert len(first.pages) == 10 and first.pages[0].rotation == 180 and first.pages[1].rotation == 90
        assert len(PdfReader(io.BytesIO(zf.read(names[2]))).pages) == 5
        # zip limit: many small files split across several zips, each under limit
        r = c.post(f"/api/jobs/{j['id']}/split", json={"pages_per_file": 1, "max_zip_mb": 0.003})
        assert r.status_code == 200, r.text
        zips = r.json()["zips"]; assert len(zips) > 1
        total = 0
        for zz in zips:
            body = c.get(zz["url"]).content
            assert len(body) <= 0.003 * 1024 * 1024
            total += len(zipfile.ZipFile(io.BytesIO(body)).namelist())
        assert total == 25
        # a single file bigger than the limit is rejected clearly
        assert c.post(f"/api/jobs/{j['id']}/split", json={"pages_per_file": 25, "max_zip_mb": 0.0002}).status_code == 400
        # expiry deletes everything
        time.sleep(3.2); m.purge_expired()
        assert c.get(f"/api/jobs/{j['id']}").status_code == 404
        assert not list(m.DATA_DIR.iterdir())


def test_bad_pdf():
    with TestClient(m.app) as c:
        assert up(c, data=b"not a pdf").status_code == 400
        assert not list(m.DATA_DIR.iterdir())
