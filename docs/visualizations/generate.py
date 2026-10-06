from pathlib import Path
import math
import zipfile
from xml.sax.saxutils import escape

from reportlab.pdfgen import canvas
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.lib.colors import HexColor
from reportlab.platypus import Paragraph
from reportlab.lib.styles import ParagraphStyle

ROOT = Path(__file__).resolve().parent
OUT = ROOT
OUT.mkdir(exist_ok=True)
PDF = OUT / 'Carbit-toteutetut-paatospuut.pdf'
SHA = '4de26b4284280fdd8c3867e01e8c38bcad718115'
URL = f'https://github.com/elissei/CarbitOy/blob/{SHA}/'
pdfmetrics.registerFont(TTFont('DV', '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf'))
pdfmetrics.registerFont(TTFont('DV-B', '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf'))
W, H = 900, 1160
C = canvas.Canvas(str(PDF), pagesize=(W, H))
C.setTitle('Carbit Oy - toteutetut päätöspuut - 4de26b4')
C.setAuthor('Carbit Oy / Elissei Hilakivi')
INK = '#213346'
MUTED = '#566779'
LINE = '#708399'
BG = '#f8fafc'
FILLS = {'process': '#eef2f6', 'decision': '#e3eef9', 'ok': '#e2f1e9', 'skip': '#f9e7e7', 'error': '#fff0d9'}
page_no = 0

def paragraph(text, x, y, width, size=14, color=INK, bold=False):
    st = ParagraphStyle('s', fontName='DV-B' if bold else 'DV', fontSize=size,
                        leading=size * 1.4, textColor=HexColor(color), spaceAfter=0)
    p = Paragraph(text, st)
    _, h = p.wrap(width, H)
    p.drawOn(C, x, H-y-h)
    return y+h

def text(s, x, y, size=14, color=INK, bold=False):
    return paragraph(escape(s).replace('\n','<br/>'), x, y, W-x-44, size, color, bold)

def page(title, subtitle):
    global page_no
    page_no += 1
    C.bookmarkPage(f'page-{page_no}')
    C.addOutlineEntry(title, f'page-{page_no}', level=0)
    C.setFillColor(HexColor(BG)); C.rect(0,0,W,H,fill=1,stroke=0)
    text('CARBIT OY  /  TOTEUTETTU LOGIIKKA',44,30,11,MUTED,True)
    text(title,44,58,26,bold=True)
    text(subtitle,44,105,13,MUTED)
    C.setStrokeColor(HexColor('#d4dde6'));C.line(44,H-148,W-44,H-148)
    text('visuals · 4de26b4 · 6.10.2026',44,H-37,10,MUTED)
    C.setFont('DV',10);C.setFillColor(HexColor(MUTED));C.drawRightString(W-44,24,f'{page_no:02d} / 19')

def finish():
    C.showPage()

def node(label, x,y,w=400,h=62,kind='process',size=14):
    C.setStrokeColor(HexColor('#b9c7d6'));C.setLineWidth(1)
    C.setFillColor(HexColor(FILLS[kind]))
    if kind=='decision':
        # Chamfered decision node keeps long predicates legible.
        p=C.beginPath();p.moveTo(x+14,H-y);p.lineTo(x+w-14,H-y);p.lineTo(x+w,H-y-h/2)
        p.lineTo(x+w-14,H-y-h);p.lineTo(x+14,H-y-h);p.lineTo(x,H-y-h/2);p.close()
        C.drawPath(p,stroke=1,fill=1)
    else:C.roundRect(x,H-y-h,w,h,8,stroke=1,fill=1)
    st=ParagraphStyle('n',fontName='DV-B' if kind=='decision' else 'DV',fontSize=size,leading=size*1.28,textColor=HexColor(INK),alignment=1)
    p=Paragraph(escape(label).replace('\n','<br/>'),st)
    _,ph=p.wrap(w-34,h)
    if ph>h-12:raise ValueError(f'Node too small: {label} ({ph} > {h})')
    p.drawOn(C,x+17,H-y-h+(h-ph)/2)
    return {'top':(x+w/2,y),'bottom':(x+w/2,y+h),'left':(x,y+h/2),'right':(x+w,y+h/2)}

def edge(a,b,label=None,via=None,label_offset=(0,0)):
    pts=[a]+(via or [])+[b]
    C.setStrokeColor(HexColor(LINE));C.setLineWidth(1.35)
    for (x1,y1),(x2,y2) in zip(pts,pts[1:]):C.line(x1,H-y1,x2,H-y2)
    x1,y1=pts[-2];x2,y2=pts[-1];ang=math.atan2(y2-y1,x2-x1);l=6
    p=C.beginPath();p.moveTo(x2,H-y2)
    p.lineTo(x2-l*math.cos(ang-.45),H-(y2-l*math.sin(ang-.45)))
    p.lineTo(x2-l*math.cos(ang+.45),H-(y2-l*math.sin(ang+.45)));p.close()
    C.setFillColor(HexColor(LINE));C.drawPath(p,stroke=0,fill=1)
    if label:
        xx=(a[0]+b[0])/2+label_offset[0];yy=(a[1]+b[1])/2+label_offset[1]
        f=10;tw=pdfmetrics.stringWidth(label,'DV',f)
        C.setFillColor(HexColor(BG));C.rect(xx-tw/2-3,H-yy-3,tw+6,15,fill=1,stroke=0)
        C.setFillColor(HexColor(MUTED));C.setFont('DV',f);C.drawCentredString(xx,H-yy,label)

def down(a,b,label=None):edge(a['bottom'],b['top'],label,label_offset=(20,0))
def right(a,b,label=None):edge(a['right'],b['left'],label,label_offset=(0,-7))
def source(paths,y=1010):
    text('Koodilähteet (klikattavat)',44,y,11,MUTED,True)
    yy=y+22
    for p in paths:
        paragraph(f'<link href="{URL+p}" color="#285d91">{escape(p)}</link>',44,yy,W-88,11)
        yy+=18

def note(title,body,y=875):
    yy=text(title,44,y,14,bold=True)
    paragraph(escape(body).replace('\n','<br/>'),44,yy+6,W-88,13,MUTED)


# Current composed application map.
page('Työtilauksesta työtarjoukseksi','Toteutettu ketju 6.10.2026 · ihmisen valinta erottaa ehdotuksen lopullisesta tarjouksesta.')
a=node('Asiakkaan viesti → tulkinta\nFake- tai OpenAI-tulkitsija',44,180,465,70)
b=node('Vaatiiko tulkinta ihmistä?\nstatus == HUMAN_REQUIRED',44,295,465,70,'decision');down(a,b)
x=node('Ihmisen käsittely\nEi työtehtävää tai ehdotusta',605,295,250,70,'skip',12);right(b,x,'Kyllä')
c=node('READY → WorkItem\nTuettu osatyyppi: brake_pad, 1 SET',44,410,465,70);down(b,c,'Ei')
d=node('Ehdotus: RawProposal\nVaraosaehdokkaat + työajan kustannus',44,525,465,70);down(c,d)
e=node('Ihminen valitsee ehdokkaan\nValitsija ja valinta-aika annetaan erikseen',44,640,465,70,'decision');down(d,e)
f=node('finalize_demo_quote()\nValinnan tarkistus → hinnoittelu → FinalQuote',44,755,465,70,'ok');down(e,f,'Erillinen kutsu')
text('Kutsuja toimittaa',605,420,15,bold=True)
paragraph('Ajoneuvon, työajan, tuotteet, toimittajatarjoukset, tunnukset ja ajankohdat. Demo ei hae näitä itse.',605,455,250,13,MUTED)
text('Esittäminen',605,635,15,bold=True)
paragraph('Hintateksti ja valinnainen AI-työkuvaus ovat erillisiä kutsuja. Demo palauttaa tarjousmallin.',605,670,250,13,MUTED)
note('Toteutuksen rajat','Yksi varaosatarve kerrallaan. Ei automaattista ostovalintaa, toimittajatilausta, Autodata-hakua tai CRM-tallennusta. Nykyinen ketju on kutsuttavia Python-funktioita; selainkäyttöliittymä ei kuulu tähän tilannekuvaan.',875)
source(['app/demo_intake_workflow.py','app/customer_intake_workflow.py','app/demo_workflow.py'],1020)
finish()

# 2: Parsing first match and exact lookup shown separately.
page('01 · Tekstistä varaosatarpeeksi','parse_part_requirement(text) - yksi sanasto-osuma muodostaa yhden tarpeen.')
text('Varaosatarpeen tulkinta',44,173,15,bold=True)
a=node('normalize_text(text)\nstrip() + lower()',44,208,430,64)
b=node('Lue synonyms.yaml\nKäy tyypit ja synonyymit YAML-järjestyksessä',44,307,430,75)
down(a,b)
c=node('Sisältyykö synonyymi tekstiin?',44,419,430,65,'decision')
down(b,c)
yes=node('Kyllä: ota ensimmäinen osuma\nLopeta molemmat hakusilmukat',44,528,430,68)
down(c,yes,'Kyllä')
more=node('Onko synonyymejä vielä jäljellä?',600,419,255,65,'decision',12)
right(c,more,'Ei')
edge(more['top'],b['right'],'Kyllä',via=[(727,391),(503,391),(503,344)],label_offset=(12,-4))
no=node('tyyppi = None',600,528,255,68,'skip')
down(more,no,'Ei')
d=node('find_axle(normalized)\nSuoritetaan myös ilman osatyyppiosumaa',44,636,430,70)
down(yes,d)
edge(no['bottom'],d['right'],via=[(727,671)])
e=node('Onko osatyyppi None?',44,749,430,62,'decision')
down(d,e)
f=node('Palauta None',600,749,255,62,'skip');right(e,f,'Kyllä')
g=node('Palauta PartRequirement\ntyyppi + akseli + määrä 1 + yksikkö SET',44,852,430,70,'ok')
down(e,g,'Ei')
note('Nykyinen tulkinta','Määrää ja yksikköä ei poimita tekstistä: tulos on aina 1 SET. Osumat ovat merkkijonojen osia, eivät kokonaisia sanoja. Esimerkiksi "öljynsuodatin" osuu ensin engine_oil-tyypin "öljy"-synonyymiin.',945)
source(['domain/ontology/parser.py','domain/ontology/synonyms.yaml'],1050)
finish()

# Exact type matching and the requires_axle flag are independent helpers.
page('02 · Sanastohakujen apupuut','find_part_type() käyttää tarkkaa yhtäsuuruutta; part_type_requires_axle() hakee säännön.')
text('find_part_type(term)',44,176,15,bold=True)
a=node('Normalisoi termi\nLue synonyms.yaml',44,217,430,65)
b=node('Seuraava synonyymi YAML-järjestyksessä',44,326,430,65);down(a,b)
c=node('Termi == normalisoitu synonyymi?',44,435,430,65,'decision',13);down(b,c)
d=node('Palauta canonical_product_type',610,435,245,65,'ok',12);right(c,d,'Kyllä')
e=node('Onko synonyymejä jäljellä?',44,544,430,65,'decision');down(c,e,'Ei')
edge(e['left'],b['left'],'Kyllä',via=[(24,576),(24,358)],label_offset=(-25,0))
f=node('Palauta None',44,653,430,65,'skip');down(e,f,'Ei')
text('part_type_requires_axle(type)',44,769,15,bold=True)
p=node('Lue part_types.yaml\nHae osatyyppi avaimella',44,811,330,70)
q=node('Löytyikö tyyppi?',440,811,190,70,'decision',12);right(p,q)
yes=node('Palauta requires_axle\nTrue tai False',440,919,190,66,'ok',12);down(q,yes,'Kyllä')
no=node('ValueError\nUnknown canonical\nproduct type',675,811,180,70,'error',11);right(q,no,'Ei')
source(['domain/ontology/parser.py','domain/ontology/synonyms.yaml','domain/ontology/part_types.yaml'],1045)
finish()

# 3: Axle parsing and type flag.
page('03 · Akselin tulkinta','find_axle(term) - ensimmäinen tarkka tai osittainen synonyymiosuma ratkaisee.')
a=node('Normalisoi termi\nLue locations.yaml',44,190,410,64)
b=node('Seuraava synonyymi\nFRONT → REAR → BOTH → UNKNOWN',44,291,410,72);down(a,b)
c=node('Termi == synonyymi?',44,403,410,62,'decision');down(b,c)
o=node('Palauta tämän ryhmän Axle-arvo',535,403,320,62,'ok');right(c,o,'Kyllä')
e=node('Sisältyykö synonyymi termiin?',44,509,410,62,'decision');down(c,e,'Ei')
o2=node('Palauta tämän ryhmän Axle-arvo',535,509,320,62,'ok');right(e,o2,'Kyllä')
f=node('Onko synonyymejä jäljellä?',44,615,410,62,'decision');down(e,f,'Ei')
edge(f['left'],b['left'],'Kyllä',via=[(24,646),(24,327)],label_offset=(-30,0))
g=node('Palauta Axle.UNKNOWN',44,721,410,62,'skip');down(f,g,'Ei')
note('Ensimmäinen osuma ratkaisee','Tässä YAML-järjestyksessä "etu ja taka" ja "edessä ja takana" palauttavat FRONT, koska etuakselin synonyymi löytyy ennen BOTH-ryhmää. "molemmissa" palauttaa BOTH. Tulkinta ei yhdistä kahta akselia.',845)
source(['domain/ontology/parser.py','domain/ontology/locations.yaml','domain/ontology/part_types.yaml'],1010)
finish()

# 4: Coverage central predicate chain.
page('04 · Hyväksytäänkö tuote kattamaan tarve?','calculate_product_coverage(requirement, product) - ehdot tarkistetaan tässä järjestyksessä.')
conds=[('Sama canonical_product_type?','Ei: hylkää kattavuus'),('Sama yksikön arvo?\nrequired_unit == package_unit','Ei: hylkää kattavuus'),('Löytyykö osatyyppi sanastosta?','Ei: ValueError'),('Vaatiiko osatyyppi akselitiedon?','Ei: suoraan määrälaskentaan'),('Onko tarpeen akseli tiedossa?\naxle != UNKNOWN','Ei: hylkää kattavuus'),('is_axle_compatible() == True?','Ei: hylkää kattavuus')]
prev=None
nodes=[]
for i,(lab,out) in enumerate(conds):
    yy=183+i*112
    n=node(lab,44,yy,465,74,'decision',13)
    if prev:down(prev,n,'Kyllä')
    side=node(out,600,yy+8,255,58,'error' if i==2 else ('process' if i==3 else 'skip'),12)
    right(n,side,'Ei')
    nodes.append((n,side));prev=n
end=node('calculate_package_coverage()\nLaske tarvittava pakkausmäärä',44,862,465,72,'ok');down(prev,end,'Kyllä')
edge(nodes[3][1]['bottom'],end['right'],via=[(873,593),(873,898)])
text('Hylkäyksen tulos: covered=False, commercial_quantity_needed=0, covered_quantity=0.',44,960,12,MUTED)
text('Tuntematon sanastotyyppi nostaa virheen vain, jos tyyppi- ja yksikkövertailut ensin läpäistään.',44,989,12,MUTED)
source(['domain/rules/package_coverage.py','domain/rules/product_matching.py','domain/rules/unit_matching.py'],1045)
finish()

# 5: Helper branches.
page('05 · Akseli- ja yksikköapufunktiot','Apufunktioiden oma käyttäytyminen ja niiden suhde pääketjuun.')
text('is_axle_compatible(required, product)',44,172,14,bold=True)
checks=['Tarpeen akseli UNKNOWN?','Tuotteen akseli UNKNOWN?','Tuotteen akseli BOTH?']
prev=None
for i,lab in enumerate(checks):
    n=node(lab,44,210+i*111,365,62,'decision',13)
    if prev:down(prev,n,'Ei')
    o=node('Palauta True',446,210+i*111,170,62,'ok',12);right(n,o,'Kyllä');prev=n
n=node('required_axle == product_axle\nPalauta vertailun True / False',44,545,365,70);down(prev,n,'Ei')
text('Pääketju torjuu tarpeen UNKNOWN-arvon\nennen tämän apufunktion kutsua.\nTuotteen UNKNOWN hyväksytään.\nTarve BOTH + tuote FRONT → False.',44,640,13,MUTED)
text('is_unit_compatible(required, product)',44,785,15,bold=True)
node('Vertaa Enum-arvoja sellaisenaan\nrequired_unit.value == product_unit.value',44,826,560,72)
text('Sama arvo → True. Eri arvo → False.\nPääketjussa ei kutsuta convert_to_pcs():\nPAIR ja PCS eivät siksi täsmää automaattisesti.',44,925,13,MUTED)
source(['domain/rules/axle_matching.py','domain/rules/unit_matching.py','domain/rules/unit_conversion.py'],1030)
finish()

# Conversion is implemented, but not wired into package coverage.
page('06 · Erillinen muunto kappaleiksi','convert_to_pcs(quantity, unit) - kaikkien toteutettujen haarojen palautukset.')
a=node('quantity > 0?',90,200,435,66,'decision')
e=node('ValueError\nquantity must be greater than 0',625,200,230,66,'error',12);right(a,e,'Ei')
b=node('unit == PCS?',90,331,435,66,'decision');down(a,b,'Kyllä')
o=node('Palauta quantity',625,331,230,66,'ok',13);right(b,o,'Kyllä')
c=node('unit == PAIR?',90,462,435,66,'decision');down(b,c,'Ei')
o2=node('Palauta quantity × 2',625,462,230,66,'ok',13);right(c,o2,'Kyllä')
d=node('ValueError\nCannot convert unit to PCS',90,593,435,76,'error');down(c,d,'Ei')
note('Yhteys pääketjuun','Funktiota ei kutsuta calculate_product_coverage()-funktiossa. Siksi esimerkiksi tarve PCS ja tuotteen package_unit PAIR hylätään yksikkövertailussa, vaikka tämä apufunktio osaa laskea parista kaksi kappaletta.',766)
text('Esimerkit: 3 PCS → 3, 1 PAIR → 2, 2 PAIR → 4.\nSET, PACKAGE, L ja KG eivät muunnu tällä funktiolla kappaleiksi.',44,913,14)
source(['domain/rules/unit_conversion.py','domain/rules/package_coverage.py','tests/test_unit_conversion.py'],1030)
finish()

# 6: Package counts.
page('07 · Pakkausmäärän laskenta','calculate_package_coverage(required_quantity, package_quantity)')
a=node('required_quantity > 0?',100,190,430,68,'decision')
ae=node('Ei: ValueError\nrequired_quantity must be greater than 0',620,190,235,68,'error',12);right(a,ae,'Ei')
b=node('package_quantity > 0?',100,311,430,68,'decision');down(a,b,'Kyllä')
be=node('Ei: ValueError\npackage_quantity must be greater than 0',620,311,235,68,'error',12);right(b,be,'Ei')
c=node('Laske kokonainen pakkausmäärä\nN = (tarve + pakkauskoko - 1) // pakkauskoko',100,447,640,80);down(b,c,'Kyllä')
d=node('Laske toimitettu määrä\ncovered_quantity = N × package_quantity',100,578,640,75);down(c,d)
e=node('Palauta PackageCoverageResult\ncovered = covered_quantity >= required_quantity\ncommercial_quantity_needed = N',100,704,640,94,'ok');down(d,e)
text('Toteutetut esimerkit',44,851,16,bold=True)
rows=[('Tarve','Pakkauksessa','Pakkauksia','Kattava määrä'),('4','4','1','4'),('4','2','2','4'),('5','2','3','6')]
for i,row in enumerate(rows):
    yy=889+i*28
    for j,val in enumerate(row):text(val,60+j*203,yy,12,bold=i==0)
note('Laskennan syöte','commercial_quantity ja commercial_unit ovat tuotemallin kenttiä, mutta tämä funktio käyttää vain required_quantity- ja package_quantity-arvoja.',1016)
source(['domain/rules/package_coverage.py'],1090)
finish()

# 7: Validation + single candidate.
page('08 · Toimittajatarjouksen validointi','validate_supplier_offer() - ensimmäinen epäonnistunut tarkistus nostaa poikkeuksen.')
text('Validointi: ensimmäinen virhe pysäyttää',44,172,14,bold=True)
gates=[('product_id.strip() ei tyhjä?','product_id must not be empty'),('supplier.strip() ei tyhjä?','supplier must not be empty'),('supplier_product_id.strip() ei tyhjä?','supplier_product_id must not be empty'),('unit_price > 0?','unit_price must be greater than 0'),('currency.strip() ei tyhjä?','currency must not be empty')]
prev=None
for i,(lab,msg) in enumerate(gates):
    n=node(lab,90,214+i*110,430,66,'decision',13)
    if prev:down(prev,n,'Kyllä')
    err=node('ValueError\n'+msg,625,214+i*110,230,66,'error',11);right(n,err,'Ei');prev=n
ok=node('Kaikki läpi → palauta None\nValidointi onnistui',90,768,430,66,'ok',13);down(prev,ok,'Kyllä')
note('strip() tarkistaa mutta ei muuta kenttää','Välilyönneistä koostuva tunnus tai valuutta hylätään. Tarkistus ei normalisoi hyväksyttyä tekstiä eikä tarkista valuutan koodia. Pääketjussa saatavuus ja tuote-ID:n vastaavuus tarkistetaan jo ennen tätä funktiota.',922)
source(['domain/rules/supplier_offer_validation.py','domain/rules/supplier_candidates.py'],1040)
finish()

page('09 · Yhden ehdokkaan muodostaminen','create_supplier_candidate(requirement, product, offer)')
a=node('validate_supplier_offer(offer)',70,200,445,66)
ae=node('ValueError\nVälittyy kutsujalle',625,200,230,66,'error',12);right(a,ae,'Virhe')
b=node('calculate_product_coverage()\nTuotetyyppi → yksikkö → akseli → pakkausmäärä',70,325,445,76);down(a,b)
be=node('Mahdollinen poikkeus\nVälittyy kutsujalle',625,325,230,76,'error',12);right(b,be,'Virhe')
c=node('coverage.covered == True?',70,461,445,66,'decision');down(b,c)
no=node('Palauta None\nEi ehdokasta',625,461,230,66,'skip',12);right(c,no,'Ei')
d=node('total_price = offer.unit_price ×\ncoverage.commercial_quantity_needed',70,583,630,75);down(c,d,'Kyllä')
e=node('Palauta SupplierCandidate\nTuote-ID tuotteesta; toimittajatunnukset,\nhinnat, valuutta ja saatavuus tarjouksesta',70,714,630,94,'ok');down(d,e)
note('Kutsun rajat','Tämä funktio ei itse tarkista saatavuutta eikä tarjouksen ja tuotteen product_id-yhtäläisyyttä. Nämä ehdot ovat build_supplier_candidates()-funktiossa. Hinta lasketaan Decimal-arvolla; tässä ei lisätä katetta, työn hintaa tai arvonlisäveroa.',901)
source(['domain/rules/supplier_candidate.py','domain/rules/package_coverage.py','domain/rules/supplier_offer_validation.py'],1040)
finish()

# 8: Outer inner loops; all acceptable offers retained.
page('10 · Kaikkien toimittajaehdokkaiden kokoaminen','build_supplier_candidates(requirement, products, offers) ja erillinen hintajärjestys.')
a=node('Aloita candidates = []\nKäy jokainen tuote ja sen jokainen tarjous läpi',44,180,470,68)
b=node('Tarjouksen ja tuotteen product_id samat?',44,290,470,66,'decision',13);down(a,b)
skip1=node('Ohita tämä tarjous\ncontinue',618,290,237,66,'skip',12);right(b,skip1,'Ei')
c=node('Saatavuus IN_STOCK tai BACKORDER?',44,400,470,66,'decision',13);down(b,c,'Kyllä')
skip2=node('OUT_OF_STOCK / UNKNOWN\nOhita tämä tarjous',618,400,237,66,'skip',12);right(c,skip2,'Ei')
d=node('create_supplier_candidate()\nValidointi → kattavuus → määrälaskenta → hinta',44,510,470,72);down(c,d,'Kyllä')
err=node('ValueError / muu poikkeus\nVälittyy ulos funktiosta',618,513,237,66,'error',12);right(d,err,'Virhe')
e=node('candidate is not None?',44,626,470,66,'decision',13);down(d,e)
skip3=node('Ei kattavuutta\nEhdokasta ei lisätä',618,626,237,66,'skip',12);right(e,skip3,'Ei')
f=node('Lisää ehdokas candidates-listaan',44,734,470,64,'ok');down(e,f,'Kyllä')
g=node('Onko tuote-tarjouspareja vielä jäljellä?',44,842,470,66,'decision',13);down(f,g)
edge(g['left'],b['left'],'Kyllä',via=[(24,875),(24,323)],label_offset=(-25,0))
end=node('Palauta candidates-lista\nEi kelvollisia ehdokkaita → []',44,957,470,65,'ok',13);down(g,end,'Ei')
for skipped in [skip1,skip2,skip3]:
    edge(skipped['bottom'],g['right'],via=[(875,skipped['bottom'][1]),(875,875)])
paragraph('sort_by_total_price()',618,940,235,13,bold=True)
paragraph('Erillinen sorted(..., key=total_price): nouseva hinta, kaikki ehdokkaat säilyvät. Sama hinta säilyttää järjestyksen. Ei valuuttamuunnosta tai ostovalintaa.',618,970,235,11,MUTED)
source(['domain/rules/supplier_candidates.py','domain/rules/availability.py','domain/rules/candidate_sorting.py'],1045)
finish()

# 9: Search loop, fake adapter and abstract contract.
page('11 · Hakuliitäntöjen toteutettu silmukka','search_suppliers(adapters, query) + FakeSupplierAdapter + SupplierAdapter')
a=node('Alusta offers = []',90,185,450,62)
b=node('Onko adaptereita vielä käsittelemättä?',90,298,450,66,'decision');down(a,b)
ret=node('Palauta offers\nTyhjä adapterilista → []',630,298,225,66,'ok',12);right(b,ret,'Ei')
c=node('Kutsu seuraavan adapterin search(query)',90,414,450,68);down(b,c,'Kyllä')
err=node('Jos kutsu nostaa poikkeuksen:\npoikkeus välittyy ulos',630,414,225,68,'error',12);right(c,err,'Virhe')
d=node('offers.extend(hakutulos)\nSäilytä tarjoukset adapterien järjestyksessä',90,535,450,72);down(c,d)
edge(d['left'],b['left'],'Seuraava adapteri',via=[(44,571),(44,331)],label_offset=(-22,0))
text('Toteutettu adapteri: FakeSupplierAdapter',44,691,16,bold=True)
text('Rakentaja tallentaa annetun offers-listan. search(query) palauttaa saman listan.\nHakuteksti ei suodata tuloksia eikä käynnistä verkkopyyntöä.',44,731,14)
text('Rajapinta: SupplierAdapter',44,816,16,bold=True)
text('Abstrakti search(query) määrittelee palautustyypiksi list[SupplierOffer].\nMetodirunko nostaa NotImplementedError-poikkeuksen. Konkreettinen adapteri\ntoteuttaa oman search-metodinsa; abstraktia luokkaa ei voi suoraan instansioida.',44,856,14)
source(['integrations/suppliers/search.py','integrations/suppliers/fake_adapter.py','integrations/suppliers/adapter.py'],1030)
finish()

# 10: All models and ontology values, ancillary I/O, complete function index.
page('12 · Varaosamallit ja sanastojen rajat','Pydantic-mallit ovat rakenteita; ne eivät itsessään tee varaosan ostovalintaa.')
rows=[
 ('Vehicle','registration, make, model; year oletuksena None'),
 ('WorkItem','operation, axle; part_requirements oletuksena uusi tyhjä lista'),
 ('PartRequirement','canonical_product_type, axle, required_quantity, required_unit'),
 ('IndexedProduct','Tuote- ja toimittajatunnukset, tyyppi, akseli; package_quantity/unit ja commercial_quantity/unit'),
 ('SupplierOffer','Tuote- ja toimittajatunnukset, unit_price (Decimal), currency, availability'),
 ('SupplierCandidate','Tunnukset, commercial_quantity_needed, unit_price/total_price (Decimal), currency, availability'),
 ('PackageCoverageResult','covered, commercial_quantity_needed, covered_quantity')]
y=178
for label,desc in rows:
    paragraph(escape(label),44,y,225,13,bold=True)
    yy=paragraph(escape(desc),281,y,574,13)
    y=max(y+35,yy+14)
text('Enum-arvot',44,525,15,bold=True)
text('Axle: FRONT, REAR, BOTH, UNKNOWN.\nUnit: PCS, SET, PAIR, PACKAGE, L, KG. RequirementUnit: samat ilman PACKAGE.\nAvailability: IN_STOCK, BACKORDER, OUT_OF_STOCK, UNKNOWN.\nOperation: BRAKE_REPLACEMENT.',44,558,13)
text('requires_axle = True',44,678,14,bold=True)
text('brake_pad, brake_disc, brake_caliper, shock_absorber, spring, control_arm,\nball_joint, stabilizer_link',44,708,13)
text('requires_axle = False',44,765,14,bold=True)
text('brake_fluid, engine_oil, oil_filter, air_filter, cabin_filter, spark_plug, battery',44,795,13)
note('load_yaml(filename) ja mallien validointi','load_yaml avaa sanastotiedoston UTF-8-muodossa ja palauttaa yaml.safe_load(file)-tuloksen. Luku-, jäsennys- ja avainvirheitä ei käsitellä erikseen. Pydantic tarkistaa mallien kenttätyypit ja enum-arvot; positiivisia määriä tai tunnettuja osatyyppejä ei ole rajoitettu suoraan mallien kentissä.',854)
source(['domain/models/indexed_product.py','domain/models/part_requirement.py','domain/models/supplier_offer.py','domain/models/work_item.py','domain/ontology/parser.py'],996)
finish()


def chain_page(title, subtitle, steps, notes, paths):
    page(title, subtitle)
    prev=None
    for i,(label,kind,branch) in enumerate(steps):
        n=node(label,44,180+i*106,465,72,kind,13)
        if prev: down(prev,n,('Ei' if 'HUMAN_REQUIRED?' in steps[i-1][0] else 'Kyllä') if steps[i-1][1]=='decision' else None)
        if branch:
            fail=node(branch,615,180+i*106,240,72,'error' if 'virhe' in branch.lower() or 'ValueError' in branch else 'skip',12)
            right(n,fail,('Kyllä' if 'HUMAN_REQUIRED?' in label else 'Ei') if kind=='decision' else 'Virhe')
        prev=n
    note('Huomio',notes,875)
    source(paths,1020)
    finish()

chain_page('13 · Viestin tulkinta ja ihmishaara',
    'process_customer_message() ja OpenAI-tulkinnan validointi',[
    ('Kutsu interpreter.interpret(message)','process','Kutsuvirhe välittyy ulos'),
    ('OpenAI-polku: JSON-objekti + Pydantic\nFake-polku: valmis tulkinta','process','JSON- tai validointivirhe\nEi intake-tulosta'),
    ('status == HUMAN_REQUIRED?','decision','Kyllä: work_item = None\nEi varaosatarpeen muuntoa'),
    ('READY: muunna tulkinta työtehtäväksi','process','Muuntovirhe välittyy ulos'),
    ('Palauta CustomerIntakeResult\nTulkinta + työtehtävä','ok',None)],
    'READY vaatii työn ja vähintään yhden osatyypin. Diagnoositarve vaatii HUMAN_REQUIRED-tilan. AI-prompti ohjaa epävarmat pyynnöt ihmiselle, mutta jokaista promptiehtoa ei validoida koodissa. Virheitä ei muuteta automaattisesti ihmishaaraksi.',
    ['app/customer_intake_workflow.py','domain/models/customer_request.py','integrations/ai/openai_customer_request_interpreter.py'])

chain_page('14 · Tulkinnasta varaosatarpeeksi',
    'build_work_item_from_customer_request() · jokainen osatyyppi käsitellään',[
    ('Onko tulkinta READY?','decision','Ei: ValueError'),
    ('Onko operation annettu?','decision','Ei: ValueError'),
    ('Jokaiselle part_types-alkiolle: löytyykö yksikkö?\nTuettu avain: brake_pad','decision','Ei: ValueError\nEi osittaista tulosta'),
    ('Lisää tarve: tyyppi + akseli\nrequired_quantity = 1, required_unit = SET','process',None),
    ('Kaikki osatyypit käsitelty → palauta WorkItem\nTyö + akseli + muodostetut tarpeet','ok',None)],
    'Jokaisesta brake_pad-esiintymästä tulee oma tarve; duplikaatteja ei poisteta. UNKNOWN-akseli kopioidaan sellaisenaan. Demo hyväksyy myöhemmin vain yhden tarpeen. Suoraan kutsuttu muuntaja ei itse tarkista osatyyppilistan tyhjyyttä.',
    ['domain/rules/customer_request_work_item.py','domain/models/customer_request.py','domain/models/part_requirement.py'])

chain_page('15 · Intake yhdistyy ehdotukseen',
    'process_demo_customer_message() → create_demo_proposal()',[
    ('Kutsu process_customer_message()','process','Tulkinta- tai muuntovirhe\nVälittyy kutsujalle'),
    ('Onko intake.work_item olemassa?','decision','Ei: palauta intake\nproposal = None'),
    ('Kaikki kuusi ehdotussyötettä annettu?\nYksikään ei saa olla None','decision','Ei: ValueError'),
    ('Onko varaosatarpeita täsmälleen yksi?','decision','Ei: ValueError'),
    ('Työkustannus = tunnit × 90 EUR\nMuodosta varaosaehdokkaat','process','Validointi- tai sääntövirhe\nVälittyy kutsujalle'),
    ('Palauta intake + RawProposal\nEi lopullista tarjousta tai osavalintaa','ok',None)],
    'Kuusi syötettä: proposal_id, vehicle, labour_time, products, offers, created_at. Tyhjät tuote- ja tarjouslistat sallitaan: ehdokaslista voi olla tyhjä. Demo ei lajittele ehdokkaita eikä hae työaikaa tai tarjouksia itse.',
    ['app/demo_intake_workflow.py','app/demo_workflow.py','domain/rules/labour_cost.py'])

chain_page('16 · Ihmisen valinnan tarkistukset',
    'finalize_demo_quote() → create_demo_final_quote() → select_candidate_from_proposal()',[
    ('Ihminen antaa ehdokkaan, valitsijan ja ajan\nKutsuja käynnistää erillisen viimeistelyn','process',None),
    ('Kuuluuko ehdokas proposal.candidates-listaan?','decision','Ei: ValueError'),
    ('Onko proposal_id ei-tyhjä strip()-käsittelyn jälkeen?','decision','Ei: ValueError'),
    ('Onko selected_by ei-tyhjä strip()-käsittelyn jälkeen?','decision','Ei: ValueError'),
    ('Muodosta HumanSelection\nEhdokkaan tiedot + valitsija + valinta-aika','process',None),
    ('Kutsu create_final_quote()\nHinnoittelu ja mallin validointi','ok',None)],
    'Jäsenyys tarkistetaan listan in-operaatiolla eli mallien yhtäsuuruudella. Tarkistus ei todenna valitsijan henkilöllisyyttä. Valinta tallennetaan palautettavaan malliin; pysyvää tietokantatallennusta tai toimittajan ostotilausta ei tehdä.',
    ['app/demo_intake_workflow.py','app/demo_workflow.py','domain/rules/human_selection.py'])

chain_page('17 · Lopullisen tarjouksen hinnoittelu',
    'create_final_quote() · laskenta tehdään domain-säännöissä',[
    ('Onko valinnan total_price > 0?','decision','Ei: ValueError'),
    ('Varaosan myyntihinta = ostohinta × 1.30\nPyöristä 0.01: ROUND_HALF_UP','process',None),
    ('Tarjouksen summa = työn hinta + varaosan myyntihinta','process',None),
    ('FinalQuote: ehdotustunnus ja ostohinta täsmäävät?','decision','Ei: validointivirhe'),
    ('Valuutat samat ja kokonaissumma oikein?','decision','Ei: validointivirhe'),
    ('Palauta FinalQuote\nValinta + hinnat + ajoneuvo + työ + aikaleima','ok',None)],
    'Kerroin 1.30 on 30 prosentin lisä ostohintaan, ei 30 prosentin myyntikate. Käytetään valinnan kokonaisostohintaa, ei yksikköhintaa. Tässä ketjussa ei lasketa erillistä ALV-erittelyä. Malli tarkistaa summan mutta ei itse uudelleenlaske kerrointa.',
    ['domain/rules/part_sale_price.py','domain/rules/final_quote.py','domain/models/final_quote.py'])

page('18 · Tarjouksen esittäminen ja AI-raja','Hintateksti ja työn kuvaus ovat erillisiä kutsuja; kutsuja yhdistää tulokset.')
a=node('FinalQuote',265,180,370,65)
b=node('format_final_quote(quote)\nKiinteä suomenkielinen hintateksti',44,300,365,80)
c=node('present_final_quote(quote, presenter)\nMuodosta QuotePresentationInput',490,300,365,80)
edge(a['bottom'],b['top'],via=[(450,270),(226,270)])
edge(a['bottom'],c['top'],via=[(450,270),(672,270)])
d=node('Ajoneuvo, varaosat, työ, summa\nEuro ja sis. ALV tulostetaan kiinteästi',44,435,365,80,'ok');down(b,d)
e=node('Valittu presenter-toteutus\nFake: annettu kuvaus\nOpenAI: operation, axle, part_types',490,435,365,100);down(c,e)
f=node('OpenAI-polku: trimmaa vastaus\nOnko kuvaus tyhjä? → ValueError',490,590,365,85,'decision');down(e,f)
g=node('QuotePresentationOutput\nVain work_description',490,725,365,70,'ok');down(f,g,'Ei')
note('AI ei laske hintoja','OpenAI-työkuvaukseen ei lähetetä ajoneuvon tunnuksia, työaikaa tai hintoja. Verkkovirheet välittyvät kutsujalle; ei retry- tai fallback-haaraa. Hintatekstin ALV-merkintä ei itsessään todista syötteiden verokäsittelyä. Muut presenter-toteutukset voivat palauttaa tyhjää tekstiä.',875)
source(['presentation/final_quote_text.py','presentation/quote_workflow.py','integrations/ai/openai_client.py'],1020)
finish()
C.save()
print(PDF)

# PNG exports from the same PDF: python generate.py (reportlab + PyMuPDF).
import fitz
png_dir = OUT / "decision-trees"
png_dir.mkdir(exist_ok=True)
with fitz.open(PDF) as document:
    for index, pdf_page in enumerate(document):
        pdf_page.get_pixmap(matrix=fitz.Matrix(1.5, 1.5)).save(png_dir / f"Carbit-paatospuu-{index+1:02d}.png")
