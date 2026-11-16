# -*- coding: utf-8 -*-
"""
Factures et reçus : ticket de caisse 80 mm et facture A4 (PDF avec reportlab, texte simple sinon).
Ce module ne dépend pas de tkinter : il peut être testé seul.
"""
import io
import os

import database_boutique as D
from database_boutique import fcfa, to_fr

try:
    from reportlab.pdfgen import canvas
    from reportlab.lib.units import mm
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.colors import HexColor, white, black
    from reportlab.lib.utils import ImageReader
    HAS_PDF = True
except ImportError:                      # pragma: no cover
    HAS_PDF = False

try:
    from reportlab.graphics.barcode import qr
    from reportlab.graphics import renderPDF
    HAS_QR = HAS_PDF
except ImportError:                      # pragma: no cover
    HAS_QR = False

NAVY, BLUE, GREY, LIGHT, LINE, RED, GREEN = '#0f2a52', '#2563eb', '#64748b', '#f1f5f9', '#d9e0ea', '#dc2626', '#16a34a'


# ------------------------------------------------------------ montant en lettres
_U = ["zéro", "un", "deux", "trois", "quatre", "cinq", "six", "sept", "huit", "neuf", "dix", "onze", "douze", "treize",
      "quatorze", "quinze", "seize", "dix-sept", "dix-huit", "dix-neuf"]
_D = ["", "", "vingt", "trente", "quarante", "cinquante", "soixante"]


def _moins_de_100(n):
    if n < 20:
        return _U[n]
    d, u = divmod(n, 10)
    if d in (7, 9):                                   # soixante-dix..., quatre-vingt-dix...
        base = "soixante" if d == 7 else "quatre-vingt"
        r = 10 + u
        return base + ("-et-" if (d == 7 and u == 1) else "-") + _U[r]
    if d == 8:
        return "quatre-vingts" if u == 0 else "quatre-vingt-" + _U[u]
    if u == 0:
        return _D[d]
    return _D[d] + ("-et-un" if u == 1 else "-" + _U[u])


def _moins_de_1000(n):
    c, r = divmod(n, 100)
    if c == 0:
        return _moins_de_100(r)
    tete = "cent" if c == 1 else _U[c] + " cent"
    if r == 0:
        return tete + ("s" if c > 1 else "")
    return tete + " " + _moins_de_100(r)


def nombre_en_lettres(n):
    """12 500 -> « douze mille cinq cents » (français, orthographe usuelle)."""
    n = int(round(n))
    if n == 0:
        return "zéro"
    if n < 0:
        return "moins " + nombre_en_lettres(-n)
    parts = []
    for taille, un, plusieurs in ((10 ** 9, "un milliard", "milliards"), (10 ** 6, "un million", "millions"), (1000, "mille", "mille")):
        q, n = divmod(n, taille)
        if q:
            if q == 1:
                parts.append(un)
            else:
                parts.append(_moins_de_1000(q) + " " + plusieurs)
    if n:
        parts.append(_moins_de_1000(n))
    return " ".join(parts)


# ------------------------------------------------------------ données de la facture
def _client(db, v):
    if not v['client_id']:
        return None
    try:
        return next((c for c in db.get_clients() if c['id'] == v['client_id']), None)
    except Exception:
        return None


def _donnees(db, vid):
    v = db.get_vente(vid)
    lignes, regs = db.get_vente_lignes(vid), [r for r in db.get_reglements(vid) if not r['annule']]
    reste = 0 if v['annule'] else max(v['total'] - v['paye'], 0)
    if v['annule']:
        statut, couleur = "ANNULÉE", RED
    elif reste <= 0.001:
        statut, couleur = "PAYÉE", GREEN
    elif v['paye'] > 0.001:
        statut, couleur = "PARTIELLEMENT PAYÉE", '#ea580c'
    else:
        statut, couleur = "À CRÉDIT", RED
    return v, lignes, regs, reste, statut, couleur, _client(db, v)


def _sortie(base_nom, ext):
    out = os.path.join(D.BASE_DIR, 'recus')
    os.makedirs(out, exist_ok=True)
    return os.path.join(out, base_nom + ext)


def _logo(cfg):
    p = (cfg.get('logo') or '').strip()
    if p and os.path.isfile(p):
        try:
            return ImageReader(p)
        except Exception:
            return None
    return None


def installer_logo(source, taille_max=600):
    """Copie le logo choisi dans le dossier de l'application sous une forme sûre (PNG, 600 px maximum, transparence conservée) :
    il ne disparaît plus si le fichier d'origine est déplacé, et s'imprime correctement sur toutes les factures.
    Retourne le chemin de la copie. Lève ValueError si le fichier n'est pas une image lisible."""
    try:
        from PIL import Image
    except ImportError:
        raise ValueError("Le module Pillow est nécessaire pour préparer le logo (pip install pillow).")
    try:
        with Image.open(source) as im:
            im.load()
            im = im.convert('RGBA')
    except Exception:
        raise ValueError("Ce fichier n'est pas une image lisible (utilisez un PNG ou un JPG).")
    im.thumbnail((taille_max, taille_max))
    os.makedirs(D.BASE_DIR, exist_ok=True)
    dest = os.path.join(D.BASE_DIR, 'logo.png')
    im.save(dest, 'PNG', optimize=True)
    return dest


def apercu_entete(format=None):
    """Document d'essai avec le logo et les coordonnées de la boutique, pour vérifier l'en-tête des factures sans faire de vente."""
    from datetime import datetime as _dt
    now = _dt.now().strftime('%Y-%m-%d %H:%M:%S')
    spec = dict(titre="APERÇU DE L'EN-TÊTE", numero="EXEMPLE", date=now, statut=("DOCUMENT D'ESSAI", GREEN),
                infos=[("Boutique", D.get_config()['boutique']), ("Logo", "affiché en haut de chaque facture"), ("Format", (format or D.get_config().get('format_recu') or 'ticket'))],
                colonnes=["Article", "Qté", "Prix unit.", "Total"], lignes=[("Cahier 200 pages", "10", "1 000", "10 000"), ("Stylo bleu", "5", "250", "1 250")],
                totaux=[("TOTAL", "11 250 FCFA", 'fort')], note="Ceci n'est pas une vente : aucun enregistrement n'a été fait.")
    return make_document(spec, "APERCU_ENTETE", format)


def _ident(cfg):
    """Ligne d'identification légale (NIU / RCCM) si renseignée."""
    t = []
    if cfg.get('niu'):
        t.append(f"NIU : {cfg['niu']}")
    if cfg.get('rccm'):
        t.append(f"RCCM : {cfg['rccm']}")
    return "   ".join(t)


def _fr(x):
    return f"{x:,.0f}".replace(',', ' ')


def _coupe(c, txt, font, size, largeur):
    """Raccourcit un texte avec « … » pour qu'il tienne dans la largeur donnée."""
    if c.stringWidth(txt, font, size) <= largeur:
        return txt
    while txt and c.stringWidth(txt + '…', font, size) > largeur:
        txt = txt[:-1]
    return txt + '…'


# ------------------------------------------------------------ facture A4
def _rendre_a4(c, db, d, cfg, duplicata, total_pages):
    v, lignes, regs, reste, statut, couleur, client = d
    W, H = A4
    M = 15 * mm
    page = [1]
    logo = _logo(cfg)

    def entete():
        c.setFillColor(HexColor(NAVY)); c.rect(0, H - 38 * mm, W, 38 * mm, stroke=0, fill=1)
        c.setFillColor(HexColor(BLUE)); c.rect(0, H - 38 * mm, W, 1.6 * mm, stroke=0, fill=1)
        x = M
        if logo:
            c.setFillColor(white); c.roundRect(M, H - 33 * mm, 24 * mm, 24 * mm, 2 * mm, stroke=0, fill=1)
            try:
                c.drawImage(logo, M + 1.5 * mm, H - 31.5 * mm, 21 * mm, 21 * mm, preserveAspectRatio=True, anchor='c', mask='auto')
            except Exception:
                pass
            x = M + 29 * mm
        taille = 18
        while taille > 11 and c.stringWidth(cfg['boutique'], 'Helvetica-Bold', taille) > (W - x - M - 52 * mm):
            taille -= 0.5
        c.setFillColor(white); c.setFont('Helvetica-Bold', taille)
        c.drawString(x, H - 17 * mm, _coupe(c, cfg['boutique'], 'Helvetica-Bold', taille, W - x - M - 52 * mm))
        c.setFont('Helvetica', 8.5); c.setFillColor(HexColor('#cbd5e1'))
        yy = H - 23 * mm
        for t in (cfg.get('adresse'), (("Tél : " + cfg['telephone']) if cfg.get('telephone') else ''), _ident(cfg)):
            if t:
                c.drawString(x, yy, _coupe(c, t, 'Helvetica', 8.5, 105 * mm)); yy -= 4.2 * mm
        c.setFillColor(white); c.setFont('Helvetica-Bold', 22); c.drawRightString(W - M, H - 17 * mm, "FACTURE")
        c.setFont('Helvetica-Bold', 11); c.drawRightString(W - M, H - 24 * mm, v['facture_no'])
        c.setFont('Helvetica', 9); c.setFillColor(HexColor('#cbd5e1'))
        c.drawRightString(W - M, H - 29.5 * mm, f"{to_fr(v['date'])}  {v['date'][11:16]}")

    def pied():
        c.setStrokeColor(HexColor(LINE)); c.line(M, 17 * mm, W - M, 17 * mm)
        c.setFillColor(HexColor(GREY)); c.setFont('Helvetica', 7.5)
        c.drawString(M, 12.5 * mm, _coupe(c, " - ".join(x for x in (cfg['boutique'], cfg.get('adresse'), cfg.get('telephone')) if x), 'Helvetica', 7.5, 130 * mm))
        c.drawRightString(W - M, 12.5 * mm, f"Page {page[0]} / {total_pages}")
        if cfg.get('mentions'):
            c.drawString(M, 8.5 * mm, _coupe(c, cfg['mentions'], 'Helvetica', 7.5, W - 2 * M))

    def en_tetes_table(y):
        c.setFillColor(HexColor(NAVY)); c.roundRect(M, y - 7.5 * mm, W - 2 * M, 8 * mm, 1.2 * mm, stroke=0, fill=1)
        c.setFillColor(white); c.setFont('Helvetica-Bold', 8.5)
        yy = y - 4.8 * mm
        c.drawString(M + 3 * mm, yy, "N°"); c.drawString(M + 13 * mm, yy, "Désignation")
        c.drawRightString(M + 118 * mm, yy, "Qté"); c.drawRightString(M + 140 * mm, yy, "Prix unit.")
        c.drawRightString(M + 156 * mm, yy, "Remise"); c.drawRightString(W - M - 3 * mm, yy, "Total")
        return y - 13 * mm

    entete()
    y = H - 46 * mm
    # bloc client / informations
    bw = (W - 2 * M - 6 * mm) / 2
    for i, (titre, rows) in enumerate((("FACTURÉ À", [client['nom'] if client else "Client de passage"] +
                                        ([f"Tél : {client['telephone']}"] if client and client['telephone'] else []) +
                                        ([client['adresse']] if client and client['adresse'] else [])),
                                       ("INFORMATIONS", [f"Vendeur : {v['vendeur']}",
                                                         "Règlement : " + (", ".join(sorted({r['mode'] for r in regs})) or "à crédit")]))):
        x = M + i * (bw + 6 * mm)
        c.setFillColor(HexColor(LIGHT)); c.setStrokeColor(HexColor(LINE)); c.roundRect(x, y - 24 * mm, bw, 24 * mm, 2 * mm, stroke=1, fill=1)
        c.setFillColor(HexColor(BLUE)); c.setFont('Helvetica-Bold', 7.5); c.drawString(x + 4 * mm, y - 5 * mm, titre)
        c.setFillColor(HexColor('#1f2933'))
        for j, t in enumerate(rows[:3]):
            c.setFont('Helvetica-Bold' if (j == 0 and i == 0) else 'Helvetica', 10 if (j == 0 and i == 0) else 9)
            c.drawString(x + 4 * mm, y - (11 + j * 5) * mm, _coupe(c, t, 'Helvetica', 10, bw - 8 * mm))
    # pastille de statut
    c.setFillColor(HexColor(couleur)); c.roundRect(W - M - 52 * mm, y - 31 * mm, 52 * mm, 6.5 * mm, 3 * mm, stroke=0, fill=1)
    c.setFillColor(white); c.setFont('Helvetica-Bold', 8.5); c.drawCentredString(W - M - 26 * mm, y - 29 * mm, statut)
    if duplicata:
        c.setFillColor(HexColor(GREY)); c.setFont('Helvetica-Bold', 9); c.drawString(M, y - 29 * mm, "DUPLICATA")
    y -= 36 * mm
    y = en_tetes_table(y)

    bas_utile = 28 * mm
    for i, l in enumerate(lignes, 1):
        if y < bas_utile + 8 * mm:
            pied(); c.showPage(); page[0] += 1; entete(); y = en_tetes_table(H - 46 * mm)
        if i % 2 == 0:
            c.setFillColor(HexColor('#f8fafc')); c.rect(M, y - 2.2 * mm, W - 2 * M, 7 * mm, stroke=0, fill=1)
        c.setFillColor(HexColor('#1f2933')); c.setFont('Helvetica', 9)
        c.drawString(M + 3 * mm, y, str(i))
        c.drawString(M + 13 * mm, y, _coupe(c, l['designation'], 'Helvetica', 9, 90 * mm))
        c.drawRightString(M + 118 * mm, y, f"{l['quantite']:g}")
        c.drawRightString(M + 140 * mm, y, _fr(l['prix_unitaire']))
        c.drawRightString(M + 156 * mm, y, f"{l['remise_pct']:g} %" if l['remise_pct'] else "-")
        c.setFont('Helvetica-Bold', 9); c.drawRightString(W - M - 3 * mm, y, _fr(l['total']))
        y -= 7 * mm
    c.setStrokeColor(HexColor(LINE)); c.line(M, y + 3 * mm, W - M, y + 3 * mm)

    # totaux : tout le bloc doit tenir sur la page, sinon page suivante
    besoin = (30 + 5 * len(regs) + (6 if v['remise'] else 0) + (6 if v['tva'] else 0)) * mm
    if y - besoin < bas_utile:
        pied(); c.showPage(); page[0] += 1; entete(); y = H - 50 * mm
    y -= 4 * mm
    # montant en lettres (à gauche)
    c.setFillColor(HexColor(GREY)); c.setFont('Helvetica', 8)
    txt = f"Arrêtée la présente facture à la somme de {nombre_en_lettres(v['total'])} francs CFA."
    mots, ligne, yy = txt.split(), "", y
    for m_ in mots:
        if c.stringWidth(ligne + " " + m_, 'Helvetica-Oblique', 8) > 92 * mm:
            c.setFont('Helvetica-Oblique', 8); c.drawString(M, yy, ligne.strip()); yy -= 4 * mm; ligne = ""
        ligne += " " + m_
    c.setFont('Helvetica-Oblique', 8); c.drawString(M, yy, ligne.strip())
    # totaux (à droite)
    tx, tw = W - M - 78 * mm, 78 * mm

    def duo(lib, val, bold=False, color='#1f2933'):
        nonlocal y
        c.setFillColor(HexColor(color)); c.setFont('Helvetica-Bold' if bold else 'Helvetica', 9.5)
        c.drawString(tx + 3 * mm, y, lib); c.drawRightString(tx + tw - 3 * mm, y, val); y -= 6 * mm

    duo("Sous-total", fcfa(v['sous_total'] if v['sous_total'] is not None else sum(l['total'] for l in lignes)))
    if v['remise']:
        duo("Remise", "- " + fcfa(v['remise']), color=RED)
    if v['tva']:
        duo("TVA", fcfa(v['tva']))
    y -= 3 * mm
    c.setFillColor(HexColor(NAVY)); c.roundRect(tx, y - 3 * mm, tw, 9 * mm, 1.5 * mm, stroke=0, fill=1)
    c.setFillColor(white); c.setFont('Helvetica-Bold', 11)
    c.drawString(tx + 3 * mm, y, "TOTAL À PAYER"); c.drawRightString(tx + tw - 3 * mm, y, fcfa(v['total'])); y -= 11 * mm
    for r in regs:
        duo(f"{r['mode']}" + (f" ({r['reference']})" if r['reference'] else ""), fcfa(r['montant']), color=GREY)
    if not v['annule']:
        duo("Reste à payer" if reste > 0.001 else "Solde", fcfa(reste), True, RED if reste > 0.001 else GREEN)
    # signature
    c.setStrokeColor(HexColor(LINE)); c.setFillColor(HexColor(GREY)); c.setFont('Helvetica', 8)
    c.drawString(M, y - 6 * mm, "Cachet et signature"); c.line(M, y - 22 * mm, M + 60 * mm, y - 22 * mm)
    if cfg.get('message_recu'):
        c.setFillColor(HexColor(BLUE)); c.setFont('Helvetica-Bold', 9); c.drawCentredString(W / 2, 23 * mm, _coupe(c, cfg['message_recu'], 'Helvetica-Bold', 9, W - 2 * M))
    pied()
    return page[0]


def _facture_a4(db, vid, duplicata):
    cfg, d = D.get_config(), _donnees(db, vid)
    chemin = _sortie(d[0]['facture_no'] + '_A4', '.pdf')
    essai = canvas.Canvas(io.BytesIO(), pagesize=A4)            # 1er passage : compter les pages
    n = _rendre_a4(essai, db, d, cfg, duplicata, 1)
    c = canvas.Canvas(chemin, pagesize=A4)
    c.setTitle(f"Facture {d[0]['facture_no']}"); c.setAuthor(cfg['boutique'])
    _rendre_a4(c, db, d, cfg, duplicata, n)
    c.save()
    return chemin


# ------------------------------------------------------------ ticket 80 mm
def _rendre_ticket(c, db, d, cfg, duplicata, H):
    v, lignes, regs, reste, statut, couleur, client = d
    W = 80 * mm
    M = 5 * mm
    y = H - 7 * mm

    def centre(txt, size=9, bold=False, color='#1f2933', pas=None, ajuste=False):
        nonlocal y
        if ajuste:                                   # réduit la police pour que le nom tienne en entier
            while size > 8 and c.stringWidth(txt, 'Helvetica-Bold' if bold else 'Helvetica', size) > W - 2 * M:
                size -= 0.5
        c.setFillColor(HexColor(color)); c.setFont('Helvetica-Bold' if bold else 'Helvetica', size)
        c.drawCentredString(W / 2, y, _coupe(c, txt, 'Helvetica-Bold' if bold else 'Helvetica', size, W - 2 * M))
        y -= pas or (size * 0.5 + 1.6) * mm * 0.9

    def pointilles():
        nonlocal y
        c.setStrokeColor(HexColor('#94a3b8')); c.setDash(1.2, 1.8); c.line(M, y + 1.2 * mm, W - M, y + 1.2 * mm); c.setDash()
        y -= 3 * mm

    logo = _logo(cfg)
    if logo:
        try:
            c.drawImage(logo, W / 2 - 9 * mm, y - 16 * mm, 18 * mm, 18 * mm, preserveAspectRatio=True, anchor='c', mask='auto')
            y -= 20 * mm
        except Exception:
            pass
    centre(cfg['boutique'], 13, True, NAVY, 6.5 * mm, ajuste=True)
    for t in (cfg.get('adresse'), ("Tél : " + cfg['telephone']) if cfg.get('telephone') else '', _ident(cfg)):
        if t:
            centre(t, 7.5, color=GREY, pas=3.4 * mm)
    y -= 1.5 * mm
    # bandeau facture
    c.setFillColor(HexColor(NAVY)); c.roundRect(M, y - 5.5 * mm, W - 2 * M, 7 * mm, 1.2 * mm, stroke=0, fill=1)
    c.setFillColor(white); c.setFont('Helvetica-Bold', 9.5)
    c.drawCentredString(W / 2, y - 3.2 * mm, ("DUPLICATA - " if duplicata else "") + f"FACTURE {v['facture_no']}")
    y -= 10 * mm
    centre(f"{to_fr(v['date'])}  {v['date'][11:16]}    Vendeur : {v['vendeur']}", 7.5, color=GREY, pas=3.6 * mm)
    if client:
        centre(f"Client : {client['nom']}", 8.5, True, pas=4 * mm)
    y -= 1 * mm
    pointilles()
    for l in lignes:
        c.setFillColor(HexColor('#1f2933')); c.setFont('Helvetica-Bold', 8.5)
        c.drawString(M, y, _coupe(c, l['designation'], 'Helvetica-Bold', 8.5, W - 2 * M)); y -= 3.8 * mm
        rem = f"  (-{l['remise_pct']:g}%)" if l['remise_pct'] else ""
        c.setFillColor(HexColor(GREY)); c.setFont('Helvetica', 8)
        c.drawString(M + 2 * mm, y, f"{l['quantite']:g} x {_fr(l['prix_unitaire'])}{rem}")
        c.setFillColor(HexColor('#1f2933')); c.setFont('Helvetica-Bold', 8.5); c.drawRightString(W - M, y, _fr(l['total'])); y -= 4.8 * mm
    pointilles()

    def duo(a, b, bold=False, size=8.5, color='#1f2933'):
        nonlocal y
        c.setFillColor(HexColor(color)); c.setFont('Helvetica-Bold' if bold else 'Helvetica', size)
        c.drawString(M, y, a); c.drawRightString(W - M, y, b); y -= (size * 0.5 + 1.8) * mm

    if v['remise']:
        duo("Remise", "- " + fcfa(v['remise']), color=RED)
    if v['tva']:
        duo("TVA", fcfa(v['tva']))
    y -= 2 * mm
    c.setFillColor(HexColor(NAVY)); c.roundRect(M, y - 2.6 * mm, W - 2 * M, 8 * mm, 1.2 * mm, stroke=0, fill=1)
    c.setFillColor(white); c.setFont('Helvetica-Bold', 11)
    c.drawString(M + 2 * mm, y, "TOTAL"); c.drawRightString(W - M - 2 * mm, y, fcfa(v['total'])); y -= 8.5 * mm
    for r in regs:
        duo(f"{r['mode']}" + (f" ({r['reference']})" if r['reference'] else ''), fcfa(r['montant']), size=8, color=GREY)
    if reste > 0.001:
        duo("RESTE À PAYER", fcfa(reste), True, 10, RED)
    y -= 1 * mm
    c.setFillColor(HexColor(couleur)); c.roundRect(W / 2 - 22 * mm, y - 3.6 * mm, 44 * mm, 5.5 * mm, 2.7 * mm, stroke=0, fill=1)
    c.setFillColor(white); c.setFont('Helvetica-Bold', 8); c.drawCentredString(W / 2, y - 2.2 * mm, statut); y -= 9 * mm
    if HAS_QR:
        try:
            w = qr.QrCodeWidget(f"{cfg['boutique']}|{v['facture_no']}|{round(v['total'])}")
            b = w.getBounds(); taille = 20 * mm
            from reportlab.graphics.shapes import Drawing
            dr = Drawing(taille, taille, transform=[taille / (b[2] - b[0]), 0, 0, taille / (b[3] - b[1]), 0, 0]); dr.add(w)
            renderPDF.draw(dr, c, W / 2 - taille / 2, y - taille + 2 * mm); y -= taille + 2 * mm
        except Exception:
            pass
    if cfg.get('message_recu'):
        centre(cfg['message_recu'], 8.5, True, BLUE, 4 * mm)
    centre("Les articles repris doivent être en bon état.", 6.5, color=GREY, pas=3 * mm)
    if cfg.get('mentions'):
        centre(cfg['mentions'], 6.5, color=GREY, pas=3 * mm)
    return H - y + 4 * mm              # hauteur utilisée


def _ticket_pdf(db, vid, duplicata):
    cfg, d = D.get_config(), _donnees(db, vid)
    chemin = _sortie(d[0]['facture_no'], '.pdf')
    grand = 600 * mm
    mesure = canvas.Canvas(io.BytesIO(), pagesize=(80 * mm, grand))      # 1er passage : mesurer la hauteur
    utile = _rendre_ticket(mesure, db, d, cfg, duplicata, grand)
    H = max(utile, 90 * mm)
    c = canvas.Canvas(chemin, pagesize=(80 * mm, H))
    c.setTitle(f"Facture {d[0]['facture_no']}"); c.setAuthor(cfg['boutique'])
    _rendre_ticket(c, db, d, cfg, duplicata, H)
    c.save()
    return chemin


# ------------------------------------------------------------ version texte (sans reportlab)
def _ticket_texte(db, vid, duplicata, largeur=42):
    cfg = D.get_config()
    v, lignes, regs, reste, statut, _, client = _donnees(db, vid)
    L = [cfg['boutique'].center(largeur)]
    for t in (cfg.get('adresse'), cfg.get('telephone'), _ident(cfg)):
        if t:
            L.append(str(t).center(largeur))
    L += ['=' * largeur, (("DUPLICATA - " if duplicata else "") + f"FACTURE {v['facture_no']}").center(largeur),
          f"{to_fr(v['date'])} {v['date'][11:16]}   Vendeur : {v['vendeur']}"]
    if client:
        L.append(f"Client : {client['nom']}")
    L.append('-' * largeur)
    for l in lignes:
        L.append(l['designation'][:largeur])
        a, b = f"   {l['quantite']:g} x {_fr(l['prix_unitaire'])}", _fr(l['total'])
        L.append(a + b.rjust(largeur - len(a)))
    L.append('-' * largeur)
    for a, b in [("TOTAL", fcfa(v['total']))] + [(r['mode'], fcfa(r['montant'])) for r in regs]:
        L.append(a + b.rjust(largeur - len(a)))
    if reste > 0.001:
        L.append("RESTE À PAYER".ljust(largeur - len(fcfa(reste))) + fcfa(reste))
    L += [statut.center(largeur), '', (cfg.get('message_recu') or '').center(largeur)]
    chemin = _sortie(v['facture_no'], '.txt')
    with open(chemin, 'w', encoding='utf-8') as f:
        f.write('\n'.join(L))
    return chemin


def make_ticket(db, vid, duplicata=False, format=None):
    """Génère la facture d'une vente. format : 'ticket' (80 mm) ou 'a4' ; par défaut celui des paramètres.
    Retourne le chemin du fichier (PDF, ou .txt si reportlab n'est pas installé)."""
    fmt = (format or D.get_config().get('format_recu') or 'ticket').lower()
    if not HAS_PDF:
        return _ticket_texte(db, vid, duplicata)
    return _facture_a4(db, vid, duplicata) if fmt == 'a4' else _ticket_pdf(db, vid, duplicata)


# ====================================================================== AUTRES DOCUMENTS (reçu de dette, dépense, réception, stock, article)
# Un seul moteur de mise en page pour tous : ticket 80 mm ou A4, selon le réglage « format de facture ».
def _rendre_doc_a4(c, cfg, spec, total_pages):
    W, H = A4
    M = 15 * mm
    page = [1]
    logo = _logo(cfg)

    def entete():
        c.setFillColor(HexColor(NAVY)); c.rect(0, H - 38 * mm, W, 38 * mm, stroke=0, fill=1)
        c.setFillColor(HexColor(BLUE)); c.rect(0, H - 38 * mm, W, 1.6 * mm, stroke=0, fill=1)
        x = M
        if logo:
            c.setFillColor(white); c.roundRect(M, H - 33 * mm, 24 * mm, 24 * mm, 2 * mm, stroke=0, fill=1)
            try:
                c.drawImage(logo, M + 1.5 * mm, H - 31.5 * mm, 21 * mm, 21 * mm, preserveAspectRatio=True, anchor='c', mask='auto')
            except Exception:
                pass
            x = M + 29 * mm
        taille = 18
        while taille > 11 and c.stringWidth(cfg['boutique'], 'Helvetica-Bold', taille) > (W - x - M - 62 * mm):
            taille -= 0.5
        c.setFillColor(white); c.setFont('Helvetica-Bold', taille)
        c.drawString(x, H - 17 * mm, _coupe(c, cfg['boutique'], 'Helvetica-Bold', taille, W - x - M - 62 * mm))
        c.setFont('Helvetica', 8.5); c.setFillColor(HexColor('#cbd5e1'))
        yy = H - 23 * mm
        for t in (cfg.get('adresse'), (("Tél : " + cfg['telephone']) if cfg.get('telephone') else ''), _ident(cfg)):
            if t:
                c.drawString(x, yy, _coupe(c, t, 'Helvetica', 8.5, W - x - M - 62 * mm)); yy -= 4.2 * mm
        c.setFillColor(white); c.setFont('Helvetica-Bold', 15); c.drawRightString(W - M, H - 16 * mm, _coupe(c, spec['titre'], 'Helvetica-Bold', 15, 80 * mm))
        c.setFont('Helvetica-Bold', 11); c.drawRightString(W - M, H - 23.5 * mm, spec['numero'])
        c.setFont('Helvetica', 9); c.setFillColor(HexColor('#cbd5e1'))
        c.drawRightString(W - M, H - 29 * mm, f"{to_fr(spec['date'])}  {spec['date'][11:16]}")

    def pied():
        c.setStrokeColor(HexColor(LINE)); c.line(M, 17 * mm, W - M, 17 * mm)
        c.setFillColor(HexColor(GREY)); c.setFont('Helvetica', 7.5)
        c.drawString(M, 12.5 * mm, _coupe(c, " - ".join(x for x in (cfg['boutique'], cfg.get('adresse'), cfg.get('telephone')) if x), 'Helvetica', 7.5, 130 * mm))
        c.drawRightString(W - M, 12.5 * mm, f"Page {page[0]} / {total_pages}")
        if cfg.get('mentions'):
            c.drawString(M, 8.5 * mm, _coupe(c, cfg['mentions'], 'Helvetica', 7.5, W - 2 * M))

    entete()
    y = H - 46 * mm
    infos = spec.get('infos', [])
    hh = (8 + 6 * len(infos)) * mm
    c.setFillColor(HexColor(LIGHT)); c.setStrokeColor(HexColor(LINE)); c.roundRect(M, y - hh, W - 2 * M, hh, 2 * mm, stroke=1, fill=1)
    yy = y - 7 * mm
    for lib, val in infos:
        c.setFillColor(HexColor(GREY)); c.setFont('Helvetica', 9); c.drawString(M + 5 * mm, yy, lib)
        c.setFillColor(HexColor('#1f2933')); c.setFont('Helvetica-Bold', 10); c.drawString(M + 48 * mm, yy, _coupe(c, str(val), 'Helvetica-Bold', 10, W - 2 * M - 55 * mm))
        yy -= 6 * mm
    y -= hh + 5 * mm
    if spec.get('statut'):
        txt, col = spec['statut']
        c.setFillColor(HexColor(col)); c.roundRect(W - M - 56 * mm, y - 6 * mm, 56 * mm, 6.5 * mm, 3 * mm, stroke=0, fill=1)
        c.setFillColor(white); c.setFont('Helvetica-Bold', 8.5); c.drawCentredString(W - M - 28 * mm, y - 4.1 * mm, txt)
    if spec.get('duplicata'):
        c.setFillColor(HexColor(GREY)); c.setFont('Helvetica-Bold', 9); c.drawString(M, y - 4 * mm, "DUPLICATA")
    y -= 10 * mm

    cols, rows = spec.get('colonnes'), spec.get('lignes', [])
    bas = 28 * mm
    if cols:
        larg = W - 2 * M
        fixe = 34 * mm
        xs = []                                           # abscisse de droite de chaque colonne (1re colonne : alignée à gauche)
        reste = larg - 6 * mm
        for k in range(len(cols) - 1, 0, -1):
            xs.insert(0, M + 3 * mm + reste if not xs else xs[0] - fixe)
        # colonnes : [libellé] + (n-1) colonnes de 34 mm alignées à droite
        def entete_table(yy):
            c.setFillColor(HexColor(NAVY)); c.roundRect(M, yy - 7.5 * mm, larg, 8 * mm, 1.2 * mm, stroke=0, fill=1)
            c.setFillColor(white); c.setFont('Helvetica-Bold', 8.5)
            c.drawString(M + 3 * mm, yy - 4.8 * mm, cols[0])
            for k, t in enumerate(cols[1:]):
                c.drawRightString(xs[k], yy - 4.8 * mm, t)
            return yy - 13 * mm
        y = entete_table(y)
        for i, r in enumerate(rows):
            if y < bas + 8 * mm:
                pied(); c.showPage(); page[0] += 1; entete(); y = entete_table(H - 46 * mm)
            if i % 2:
                c.setFillColor(HexColor('#f8fafc')); c.rect(M, y - 2.2 * mm, larg, 7 * mm, stroke=0, fill=1)
            c.setFillColor(HexColor('#1f2933')); c.setFont('Helvetica', 9)
            c.drawString(M + 3 * mm, y, _coupe(c, str(r[0]), 'Helvetica', 9, xs[0] - fixe - M - 6 * mm if len(xs) else larg))
            for k, v in enumerate(r[1:]):
                c.setFont('Helvetica-Bold' if k == len(r) - 2 else 'Helvetica', 9)
                c.drawRightString(xs[k], y, _coupe(c, str(v), 'Helvetica', 9, fixe - 3 * mm))
            y -= 7 * mm
        c.setStrokeColor(HexColor(LINE)); c.line(M, y + 3 * mm, W - M, y + 3 * mm)
    tot = spec.get('totaux', [])
    if y - (22 + 7 * len(tot)) * mm < bas:
        pied(); c.showPage(); page[0] += 1; entete(); y = H - 50 * mm
    y -= 4 * mm
    tx, tw = W - M - 90 * mm, 90 * mm
    for lib, val, style in tot:
        if style == 'fort':
            y -= 2 * mm
            c.setFillColor(HexColor(NAVY)); c.roundRect(tx, y - 3 * mm, tw, 9 * mm, 1.5 * mm, stroke=0, fill=1)
            c.setFillColor(white); c.setFont('Helvetica-Bold', 11.5)
            c.drawString(tx + 3 * mm, y, lib); c.drawRightString(tx + tw - 3 * mm, y, val); y -= 11 * mm
        else:
            col = {'rouge': RED, 'vert': GREEN}.get(style, '#1f2933')
            c.setFillColor(HexColor(col)); c.setFont('Helvetica-Bold' if style in ('rouge', 'vert') else 'Helvetica', 9.5)
            c.drawString(tx + 3 * mm, y, lib); c.drawRightString(tx + tw - 3 * mm, y, val); y -= 6.5 * mm
    if spec.get('note'):
        c.setFillColor(HexColor(GREY)); c.setFont('Helvetica-Oblique', 8.5)
        c.drawString(M, y + 4 * mm, _coupe(c, spec['note'], 'Helvetica-Oblique', 8.5, 95 * mm))
    if spec.get('signature'):
        c.setStrokeColor(HexColor(LINE)); c.setFillColor(HexColor(GREY)); c.setFont('Helvetica', 8)
        c.drawString(M, y - 6 * mm, spec['signature']); c.line(M, y - 22 * mm, M + 60 * mm, y - 22 * mm)
    pied()
    return page[0]


def _rendre_doc_ticket(c, cfg, spec, H):
    W, M = 80 * mm, 5 * mm
    y = H - 7 * mm

    def centre(txt, size=9, bold=False, color='#1f2933', pas=None, ajuste=False):
        nonlocal y
        f = 'Helvetica-Bold' if bold else 'Helvetica'
        while ajuste and size > 8 and c.stringWidth(txt, f, size) > W - 2 * M:
            size -= 0.5
        c.setFillColor(HexColor(color)); c.setFont(f, size)
        c.drawCentredString(W / 2, y, _coupe(c, txt, f, size, W - 2 * M))
        y -= pas or (size * 0.5 + 1.6) * mm * 0.9

    def pointilles():
        nonlocal y
        c.setStrokeColor(HexColor('#94a3b8')); c.setDash(1.2, 1.8); c.line(M, y + 1.2 * mm, W - M, y + 1.2 * mm); c.setDash()
        y -= 3 * mm

    logo = _logo(cfg)
    if logo:
        try:
            c.drawImage(logo, W / 2 - 9 * mm, y - 16 * mm, 18 * mm, 18 * mm, preserveAspectRatio=True, anchor='c', mask='auto'); y -= 20 * mm
        except Exception:
            pass
    centre(cfg['boutique'], 13, True, NAVY, 6.5 * mm, ajuste=True)
    for t in (cfg.get('adresse'), ("Tél : " + cfg['telephone']) if cfg.get('telephone') else '', _ident(cfg)):
        if t:
            centre(t, 7.5, color=GREY, pas=3.4 * mm)
    y -= 1.5 * mm
    c.setFillColor(HexColor(NAVY)); c.roundRect(M, y - 5.5 * mm, W - 2 * M, 7 * mm, 1.2 * mm, stroke=0, fill=1)
    c.setFillColor(white); c.setFont('Helvetica-Bold', 9)
    c.drawCentredString(W / 2, y - 3.2 * mm, _coupe(c, ("DUPLICATA - " if spec.get('duplicata') else "") + spec['titre'], 'Helvetica-Bold', 9, W - 2 * M - 4 * mm))
    y -= 10 * mm
    centre(f"{spec['numero']}   {to_fr(spec['date'])} {spec['date'][11:16]}", 8, True, pas=4 * mm)
    y -= 1 * mm
    pointilles()
    for lib, val in spec.get('infos', []):
        c.setFillColor(HexColor(GREY)); c.setFont('Helvetica', 7.5); c.drawString(M, y, lib)
        c.setFillColor(HexColor('#1f2933')); c.setFont('Helvetica-Bold', 8.5)
        c.drawRightString(W - M, y, _coupe(c, str(val), 'Helvetica-Bold', 8.5, W - 2 * M - c.stringWidth(lib, 'Helvetica', 7.5) - 3 * mm)); y -= 4.4 * mm
    if spec.get('lignes'):
        pointilles()
        for r in spec['lignes']:
            c.setFillColor(HexColor('#1f2933')); c.setFont('Helvetica-Bold', 8.5)
            c.drawString(M, y, _coupe(c, str(r[0]), 'Helvetica-Bold', 8.5, W - 2 * M)); y -= 3.8 * mm
            c.setFillColor(HexColor(GREY)); c.setFont('Helvetica', 7.8)
            c.drawString(M + 2 * mm, y, _coupe(c, "  |  ".join(str(x) for x in r[1:-1]), 'Helvetica', 7.8, 45 * mm))
            c.setFillColor(HexColor('#1f2933')); c.setFont('Helvetica-Bold', 8.5); c.drawRightString(W - M, y, str(r[-1])); y -= 4.8 * mm
    pointilles()
    for lib, val, style in spec.get('totaux', []):
        if style == 'fort':
            y -= 2 * mm
            c.setFillColor(HexColor(NAVY)); c.roundRect(M, y - 2.6 * mm, W - 2 * M, 8 * mm, 1.2 * mm, stroke=0, fill=1)
            c.setFillColor(white); c.setFont('Helvetica-Bold', 10)
            c.drawString(M + 2 * mm, y, lib); c.drawRightString(W - M - 2 * mm, y, val); y -= 8.5 * mm
        else:
            col = {'rouge': RED, 'vert': GREEN}.get(style, '#1f2933')
            c.setFillColor(HexColor(col)); c.setFont('Helvetica-Bold' if style in ('rouge', 'vert') else 'Helvetica', 8.5)
            c.drawString(M, y, lib); c.drawRightString(W - M, y, val); y -= 4.6 * mm
    if spec.get('statut'):
        y -= 1.5 * mm
        c.setFillColor(HexColor(spec['statut'][1])); c.roundRect(W / 2 - 24 * mm, y - 3.6 * mm, 48 * mm, 5.5 * mm, 2.7 * mm, stroke=0, fill=1)
        c.setFillColor(white); c.setFont('Helvetica-Bold', 8); c.drawCentredString(W / 2, y - 2.2 * mm, spec['statut'][0]); y -= 9 * mm
    if spec.get('signature'):
        y -= 3 * mm
        c.setFillColor(HexColor(GREY)); c.setFont('Helvetica', 7.5); c.drawString(M, y, spec['signature']); c.line(M, y - 10 * mm, W - M, y - 10 * mm); y -= 14 * mm
    if spec.get('note'):
        centre(spec['note'], 7, color=GREY, pas=3.4 * mm)
    if cfg.get('mentions'):
        centre(cfg['mentions'], 6.5, color=GREY, pas=3 * mm)
    return H - y + 4 * mm


def make_document(spec, nom, format=None):
    """Génère un document décrit par `spec` (titre, numero, date, infos, colonnes, lignes, totaux, statut, signature, note).
    Retourne le chemin du fichier (PDF, ou .txt sans reportlab)."""
    cfg = D.get_config()
    fmt = (format or cfg.get('format_recu') or 'ticket').lower()
    if not HAS_PDF:
        L = [cfg['boutique'].center(42), '=' * 42, spec['titre'].center(42), f"{spec['numero']}  {to_fr(spec['date'])} {spec['date'][11:16]}", '-' * 42]
        L += [f"{a} : {b}" for a, b in spec.get('infos', [])] + ['-' * 42]
        L += ["  ".join(str(x) for x in r) for r in spec.get('lignes', [])]
        L += [f"{a} : {b}" for a, b, _ in spec.get('totaux', [])]
        chemin = _sortie(nom, '.txt')
        with open(chemin, 'w', encoding='utf-8') as f:
            f.write('\n'.join(L))
        return chemin
    if fmt == 'a4':
        chemin = _sortie(nom + '_A4', '.pdf')
        n = _rendre_doc_a4(canvas.Canvas(io.BytesIO(), pagesize=A4), cfg, spec, 1)
        c = canvas.Canvas(chemin, pagesize=A4); c.setTitle(f"{spec['titre']} {spec['numero']}"); c.setAuthor(cfg['boutique'])
        _rendre_doc_a4(c, cfg, spec, n); c.save()
        return chemin
    chemin = _sortie(nom, '.pdf')
    grand = 600 * mm
    utile = _rendre_doc_ticket(canvas.Canvas(io.BytesIO(), pagesize=(80 * mm, grand)), cfg, spec, grand)
    H = max(utile, 80 * mm)
    c = canvas.Canvas(chemin, pagesize=(80 * mm, H)); c.setTitle(f"{spec['titre']} {spec['numero']}"); c.setAuthor(cfg['boutique'])
    _rendre_doc_ticket(c, cfg, spec, H); c.save()
    return chemin


# ---------------------------------------------------------------- les 5 documents automatiques
def _v(row, k, d=''):
    try:
        return row[k] if row[k] is not None else d
    except (KeyError, IndexError, TypeError):
        return d


def recu_reglement(db, rid, duplicata=False, format=None):
    """Reçu remis au client quand il règle (tout ou partie) une dette."""
    g = db.get_reglement(rid)
    mode = g['mode'] + (f" ({g['reference']})" if g['reference'] else '')
    solde = g['reste'] <= 0.001
    spec = dict(titre="REÇU DE PAIEMENT", numero=f"REC-{rid:06d}", date=g['date'], duplicata=duplicata,
                statut=("DETTE SOLDÉE", GREEN) if solde else ("PAIEMENT PARTIEL", '#ea580c'),
                infos=[("Facture", g['facture_no']), ("Client", g['client'] or "Client de passage"), ("Mode de paiement", mode), ("Encaissé par", g['caissier'])],
                totaux=[("Montant encaissé", fcfa(g['montant']), 'fort'), ("Total de la facture", fcfa(g['total_facture']), ''),
                        ("Total déjà payé", fcfa(g['paye_total']), ''), ("Reste à payer" if not solde else "Solde", fcfa(g['reste']), 'vert' if solde else 'rouge')],
                signature="Cachet et signature", note="Ce reçu atteste du paiement ci-dessus.")
    return make_document(spec, f"REC-{rid:06d}", format)


def bon_depense(db, did, duplicata=False, format=None):
    d = db.get_depense(did)
    spec = dict(titre="BON DE DÉPENSE", numero=f"DEP-{did:06d}", date=d['date'], duplicata=duplicata,
                infos=[("Motif", d['libelle']), ("Catégorie", _v(d, 'categorie', '-') or '-'), ("Enregistré par", d['par'])],
                totaux=[("Montant dépensé", fcfa(d['montant']), 'fort')], signature="Signature du bénéficiaire", note="Sortie de caisse justifiée par ce bon.")
    return make_document(spec, f"DEP-{did:06d}", format)


def bon_reception(db, achat_id, duplicata=False, format=None):
    a = db.get_achat(achat_id)
    lignes = [(l['designation'] or '?', f"{l['quantite']:g}", _fr(l['prix_achat']), _fr(l['total'])) for l in db.get_achat_lignes(achat_id)]
    reste = max(a['total'] - a['paye'], 0)
    spec = dict(titre="BON DE RÉCEPTION", numero=a['bon_no'], date=a['date'], duplicata=duplicata,
                statut=("PAYÉ", GREEN) if reste <= 0.001 else ("À PAYER AU FOURNISSEUR", '#ea580c'),
                infos=[("Fournisseur", a['fournisseur'] or '-'), ("Réf. fournisseur", _v(a, 'reference_fournisseur', '-') or '-'), ("Reçu par", a['par'])],
                colonnes=["Article", "Qté", "Prix d'achat", "Total"], lignes=lignes,
                totaux=[("TOTAL", fcfa(a['total']), 'fort'), ("Payé", fcfa(a['paye']), ''), ("Reste dû", fcfa(reste), 'vert' if reste <= 0.001 else 'rouge')],
                signature="Visa du magasinier")
    return make_document(spec, a['bon_no'], format)


def bon_ajustement(db, mid, duplicata=False, format=None):
    m = db.get_mouvement(mid)
    nom = {'perte': "Perte / casse", 'ajustement': "Ajustement"}.get(m['type'], m['type'])
    spec = dict(titre="BON D'AJUSTEMENT DE STOCK", numero=f"MVT-{mid:06d}", date=m['date'], duplicata=duplicata,
                infos=[("Article", f"{m['designation']} ({m['code']})"), ("Nature", nom), ("Motif", _v(m, 'motif', '-') or '-'), ("Fait par", m['par'])],
                totaux=[("Quantité", f"{m['quantite']:+g}", 'fort'), ("Stock après opération", f"{m['stock_apres']:g}", '')], signature="Visa du responsable")
    return make_document(spec, f"MVT-{mid:06d}", format)


def fiche_article(db, aid, avant=None, format=None):
    """Création d'un article (fiche complète) ou modification (liste des changements : avant / après)."""
    a = db.get_article(aid)
    cats = {c['id']: c['nom'] for c in db.get_categories()}
    now = __import__('datetime').datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    champs = [('designation', "Désignation", str), ('code', "Code", str), ('categorie_id', "Catégorie", lambda v: cats.get(v, v)),
              ('prix_vente', "Prix de vente", lambda v: fcfa(v or 0)), ('prix_achat', "Prix d'achat", lambda v: fcfa(v or 0)),
              ('seuil_alerte', "Seuil d'alerte", lambda v: f"{v or 0:g}"), ('auteur', "Auteur", str), ('editeur', "Éditeur", str), ('niveau', "Niveau", str)]
    if avant is None:
        infos = [(lib, f(_v(a, k, ''))) for k, lib, f in champs if _v(a, k, '') not in ('', None)] + [("Stock initial", f"{_v(a, 'stock', 0):g}")]
        spec = dict(titre="FICHE ARTICLE - NOUVEAU", numero=f"ART-{_v(a, 'code')}", date=now, infos=infos[:10], signature="Visa du magasinier",
                    totaux=[("Prix de vente", fcfa(a['prix_vente']), 'fort')])
        return make_document(spec, f"ART-{aid:06d}", format)
    lignes = []
    for k, lib, f in champs:
        av, ap = _v(avant, k, ''), _v(a, k, '')
        if str(av) != str(ap):
            lignes.append((lib, f(av) if av != '' else '-', f(ap) if ap != '' else '-'))
    if not lignes:
        lignes = [("Aucun champ modifié", "-", "-")]
    spec = dict(titre="MODIFICATION D'ARTICLE", numero=f"ART-{_v(a, 'code')}", date=now,
                infos=[("Article", a['designation']), ("Code", _v(a, 'code')), ("Stock actuel", f"{_v(a, 'stock', 0):g}")],
                colonnes=["Champ modifié", "Avant", "Après"], lignes=lignes, signature="Visa du responsable",
                note="Valeur avant à gauche, valeur après à droite (ticket).",
                totaux=[("Nombre de champs modifiés", str(len(lignes)) if lignes[0][0] != "Aucun champ modifié" else "0", '')])
    return make_document(spec, f"ART-{aid:06d}-{now[11:13]}{now[14:16]}{now[17:19]}", format)


# ====================================================================== ÉTATS EN TABLEAU (inventaire, journal d'audit) - A4 paysage
def _rendre_tableau(c, cfg, spec, total_pages):
    """Moteur de tableaux multi-pages : en-tête répété, lignes alternées, lignes de groupe / sous-total / total, texte long à la ligne."""
    from reportlab.lib.pagesizes import landscape
    from reportlab.lib.utils import simpleSplit
    W, H = landscape(A4)
    M = 12 * mm
    cols = spec['colonnes']                                   # [(titre, poids, alignement 'l' | 'r' | 'c', retour_ligne)]
    total_poids = sum(x[1] for x in cols)
    xs, x = [], M
    for _, p, _, _ in cols:
        w = (W - 2 * M) * p / total_poids
        xs.append((x, w)); x += w
    FS = 8
    page = [1]

    logo = _logo(cfg)

    def entete_page():
        c.setFillColor(HexColor(NAVY)); c.rect(0, H - 22 * mm, W, 22 * mm, stroke=0, fill=1)
        c.setFillColor(HexColor(BLUE)); c.rect(0, H - 22 * mm, W, 1.2 * mm, stroke=0, fill=1)
        x = M
        if logo:                                                     # logo de l'entreprise en haut à gauche, sur fond blanc
            c.setFillColor(white); c.roundRect(M, H - 19.5 * mm, 16 * mm, 16 * mm, 1.5 * mm, stroke=0, fill=1)
            try:
                c.drawImage(logo, M + 1 * mm, H - 18.5 * mm, 14 * mm, 14 * mm, preserveAspectRatio=True, anchor='c', mask='auto')
            except Exception:
                pass
            x = M + 20 * mm
        c.setFillColor(white); c.setFont('Helvetica-Bold', 14)
        c.drawString(x, H - 11 * mm, _coupe(c, cfg['boutique'], 'Helvetica-Bold', 14, 130 * mm))
        c.setFont('Helvetica', 8); c.setFillColor(HexColor('#cbd5e1'))
        c.drawString(x, H - 17 * mm, _coupe(c, " - ".join(t for t in (cfg.get('adresse'), cfg.get('telephone'), _ident(cfg)) if t), 'Helvetica', 8, 150 * mm))
        c.setFillColor(white); c.setFont('Helvetica-Bold', 13); c.drawRightString(W - M, H - 11 * mm, spec['titre'])
        c.setFont('Helvetica', 9); c.setFillColor(HexColor('#cbd5e1')); c.drawRightString(W - M, H - 17 * mm, spec['sous_titre'])

    def pied():
        c.setStrokeColor(HexColor(LINE)); c.line(M, 14 * mm, W - M, 14 * mm)
        c.setFillColor(HexColor(GREY)); c.setFont('Helvetica', 7.5)
        c.drawString(M, 10 * mm, _coupe(c, spec.get('pied', ''), 'Helvetica', 7.5, 200 * mm))
        c.drawRightString(W - M, 10 * mm, f"Page {page[0]} / {total_pages}")

    def entete_tableau(y):
        c.setFillColor(HexColor(NAVY)); c.rect(M, y - 6.5 * mm, W - 2 * M, 7 * mm, stroke=0, fill=1)
        c.setFillColor(white); c.setFont('Helvetica-Bold', FS)
        for (titre, _, al, _), (x0, w) in zip(cols, xs):
            _ecrire(c, titre, x0, w, y - 4.3 * mm, al)
        return y - 7.5 * mm

    entete_page()
    y = H - 28 * mm
    c.setFillColor(HexColor('#1f2933')); c.setFont('Helvetica', 9)
    for ligne in spec.get('infos', []):
        c.drawString(M, y, ligne); y -= 4.6 * mm
    y -= 2 * mm
    y = entete_tableau(y)
    bas = 20 * mm
    zebra = 0
    for ligne in spec['lignes']:
        style, cells = ligne.get('style', 'normal'), ligne['cells']
        if style == 'groupe':
            h = 6 * mm; morceaux = [[str(cells[0])]]
        else:
            morceaux = []
            for (titre, _, al, wrap), (x0, w), v in zip(cols, xs, cells):
                txt = str(v)
                morceaux.append(simpleSplit(txt, 'Helvetica', FS, w - 3 * mm) or [''] if wrap else [txt])
            h = max(len(m) for m in morceaux) * 3.7 * mm + 2.2 * mm
        if y - h < bas:
            pied(); c.showPage(); page[0] += 1; entete_page(); y = entete_tableau(H - 28 * mm)
        if style == 'groupe':
            c.setFillColor(HexColor('#dbe6f7')); c.rect(M, y - h + 0.5 * mm, W - 2 * M, h - 0.5 * mm, stroke=0, fill=1)
            c.setFillColor(HexColor(NAVY)); c.setFont('Helvetica-Bold', 8.5); c.drawString(M + 1.5 * mm, y - 4 * mm, str(cells[0]))
        else:
            if style == 'total':
                c.setFillColor(HexColor(NAVY)); c.rect(M, y - h + 0.5 * mm, W - 2 * M, h - 0.5 * mm, stroke=0, fill=1); couleur, gras = white, True
            elif style == 'sous':
                c.setFillColor(HexColor('#eef2f8')); c.rect(M, y - h + 0.5 * mm, W - 2 * M, h - 0.5 * mm, stroke=0, fill=1); couleur, gras = HexColor('#1f2933'), True
            else:
                zebra += 1
                if zebra % 2 == 0:
                    c.setFillColor(HexColor('#f8fafc')); c.rect(M, y - h + 0.5 * mm, W - 2 * M, h - 0.5 * mm, stroke=0, fill=1)
                couleur, gras = HexColor('#1f2933'), False
            f = 'Helvetica-Bold' if gras else 'Helvetica'
            c.setFillColor(couleur); c.setFont(f, FS)
            for (titre, _, al, wrap), (x0, w), lignes_txt in zip(cols, xs, morceaux):
                for k, t in enumerate(lignes_txt):
                    _ecrire(c, t if wrap else _coupe(c, t, f, FS, w - 3 * mm), x0, w, y - 3.4 * mm - k * 3.7 * mm, al)
        y -= h
    # résumé + signatures (sur la même page si la place suffit)
    resume = spec.get('resume', [])
    besoin = (16 if resume else 0) + (26 if spec.get('signatures') else 0)
    if y - besoin * mm < bas:
        pied(); c.showPage(); page[0] += 1; entete_page(); y = H - 28 * mm
    if resume:
        y -= 5 * mm
        n = len(resume)
        bw = (W - 2 * M - (n - 1) * 3 * mm) / n
        for i, (lib, val) in enumerate(resume):
            x0 = M + i * (bw + 3 * mm)
            c.setFillColor(HexColor(LIGHT)); c.setStrokeColor(HexColor(LINE)); c.roundRect(x0, y - 11 * mm, bw, 12 * mm, 1.5 * mm, stroke=1, fill=1)
            c.setFillColor(HexColor(GREY)); c.setFont('Helvetica', 7); c.drawString(x0 + 2 * mm, y - 3.5 * mm, _coupe(c, lib, 'Helvetica', 7, bw - 4 * mm))
            c.setFillColor(HexColor(NAVY)); c.setFont('Helvetica-Bold', 10); c.drawString(x0 + 2 * mm, y - 8.5 * mm, _coupe(c, str(val), 'Helvetica-Bold', 10, bw - 4 * mm))
        y -= 15 * mm
    if spec.get('note'):
        c.setFillColor(HexColor(GREY)); c.setFont('Helvetica-Oblique', 8); c.drawString(M, y, spec['note']); y -= 5 * mm
    for k, lib in enumerate(spec.get('signatures', [])):
        sx = M + k * 80 * mm
        c.setFillColor(HexColor(GREY)); c.setFont('Helvetica', 8); c.drawString(sx, y - 6 * mm, lib)
        c.setStrokeColor(HexColor(LINE)); c.line(sx, y - 19 * mm, sx + 65 * mm, y - 19 * mm)
    pied()
    return page[0]


def _ecrire(c, txt, x0, w, y, al):
    if al == 'r':
        c.drawRightString(x0 + w - 1.5 * mm, y, txt)
    elif al == 'c':
        c.drawCentredString(x0 + w / 2, y, txt)
    else:
        c.drawString(x0 + 1.5 * mm, y, txt)


def pdf_tableau(spec, nom):
    """Génère un état en tableau (A4 paysage) décrit par `spec`. Retourne le chemin du PDF."""
    if not HAS_PDF:
        raise RuntimeError("le module reportlab est nécessaire pour créer ce PDF (pip install reportlab)")
    from reportlab.lib.pagesizes import landscape
    cfg = D.get_config()
    chemin = _sortie(nom, '.pdf')
    n = _rendre_tableau(canvas.Canvas(io.BytesIO(), pagesize=landscape(A4)), cfg, spec, 1)
    c = canvas.Canvas(chemin, pagesize=landscape(A4)); c.setTitle(f"{spec['titre']} - {spec['sous_titre']}"); c.setAuthor(cfg['boutique'])
    _rendre_tableau(c, cfg, spec, n)
    c.save()
    return chemin


def _q(x):
    return f"{x:,.0f}".replace(',', ' ') if abs(x - round(x)) < 1e-9 else f"{x:,.2f}".replace(',', ' ').replace('.', ',')


def rapport_inventaire(db, d1, d2, tout=False, journaliser=True):
    """Inventaire de la boutique pour la période d1..d2 en PDF (A4 paysage) ; l'édition est inscrite au journal d'audit."""
    inv = db.inventaire_periode(d1, d2, tout)
    t = inv['totaux']
    lignes, cat_courante, bloc = [], None, []
    def clore():
        if bloc:
            c = next(x for x in inv['categories'] if x['categorie'] == cat_courante)
            lignes.append(dict(style='sous', cells=['', f"Sous-total {cat_courante} ({c['nb']} art.)", _q(c['debut']), _q(sum(x['achats'] for x in bloc)), _q(sum(x['retours'] for x in bloc)),
                                              _q(sum(x['ventes'] for x in bloc)), (lambda a: f"{a:+g}" if a else "0")(sum(x['ajust'] for x in bloc)), _q(c['fin']), '',
                                              _fr(c['valeur_achat']), _fr(c['valeur_vente'])]))
    for l in inv['lignes']:
        if l['categorie'] != cat_courante:
            clore(); bloc = []; cat_courante = l['categorie']
            lignes.append(dict(style='groupe', cells=[cat_courante]))
        bloc.append(l)
        lignes.append(dict(cells=[l['code'], l['designation'] + ("  (archivé)" if not l['actif'] else "") + (" *" if abs(l['ecart']) > 1e-9 else ""), _q(l['debut']), _q(l['achats']), _q(l['retours']), _q(l['ventes']),
                                  f"{l['ajust']:+g}" if l['ajust'] else "0", _q(l['fin']), _fr(l['prix_achat']), _fr(l['valeur_achat']), _fr(l['valeur_vente'])]))
    clore()
    lignes.append(dict(style='total', cells=['', f"TOTAL GÉNÉRAL ({t['nb']} articles)", _q(t['debut']), _q(t['achats']), _q(t['retours']), _q(t['ventes']),
                                             f"{t['ajust']:+g}" if t['ajust'] else "0", _q(t['fin']), '', _fr(t['valeur_achat']), _fr(t['valeur_vente'])]))
    spec = dict(titre="INVENTAIRE DE LA BOUTIQUE", sous_titre=f"Période du {to_fr(d1)} au {to_fr(d2)}",
                pied=f"Établi le {to_fr(inv['genere'])} {inv['genere'][11:16]} par {inv['par']} - valorisation au prix d'achat / de vente actuel de chaque article",
                infos=[f"Période : du {to_fr(d1)} au {to_fr(d2)}   |   {t['nb']} article(s) {'(tous)' if tout else 'avec du stock ou des mouvements'}   |   Édité par : {inv['par']}"],
                colonnes=[("Code", 1.5, 'l', False), ("Désignation", 4.6, 'l', True), ("Début", 0.9, 'r', False), ("Achats", 0.9, 'r', False), ("Retours", 0.9, 'r', False),
                          ("Ventes", 0.9, 'r', False), ("Pertes/Ajust.", 1.2, 'r', False), ("Fin", 0.9, 'r', False), ("Prix d'achat", 1.4, 'r', False),
                          ("Valeur achat", 1.7, 'r', False), ("Valeur vente", 1.7, 'r', False)],
                lignes=lignes,
                resume=[("Articles", str(t['nb'])), ("Stock au début (unités)", _q(t['debut'])), ("Stock à la fin (unités)", _q(t['fin'])),
                        ("Valeur début (achat)", fcfa(t['valeur_debut'])), ("Valeur fin à l'achat", fcfa(t['valeur_achat'])),
                        ("Valeur fin à la vente", fcfa(t['valeur_vente'])), ("Marge potentielle", fcfa(t['marge_potentielle']))],
                note=("* ATTENTION : stock non justifié par l'historique (modifié hors de l'application) pour : " +
                      ", ".join(f"{a['code']} (écart {a['ecart']:+g})" for a in inv['anomalies'][:6]) + (" ..." if len(inv['anomalies']) > 6 else "") + "   |   ") if inv['anomalies'] else "" +
                     "Stock fin = stock réel à la date de fin ; stock début = stock fin moins les mouvements de la période. Pertes/Ajust. : pertes, casse, ajustements et comptages (+/-).",
                signatures=["Établi par", "Vérifié par le gérant", "Date et visa"])
    chemin = pdf_tableau(spec, f"INVENTAIRE_{d1}_{d2}")
    if journaliser:
        db.journaliser_inventaire(d1, d2, tout)                    # trace dans le journal d'audit
    return chemin


def journal_audit_pdf(db, d1, d2, term='', journaliser=True):
    """Journal d'audit de la période en PDF (A4 paysage), avec le résultat du contrôle d'intégrité."""
    rows = list(reversed(db.get_audit(term, d1, d2, limit=100000)))
    ok, msg = db.verify_audit()
    lignes = [dict(cells=[str(r['id']), f"{to_fr(r['date'])} {r['date'][11:16]}", r['utilisateur'], r['action'], r['details'] or '']) for r in rows] or \
             [dict(cells=['', '', '', "Aucune entrée sur cette période", ''])]
    spec = dict(titre="JOURNAL D'AUDIT", sous_titre=f"Période du {to_fr(d1)} au {to_fr(d2)}",
                pied=f"Édité le {__import__('datetime').datetime.now():%d/%m/%Y %H:%M} par {db.actor} - chaque entrée est chaînée à la précédente (empreinte SHA-256)",
                infos=[f"Période : du {to_fr(d1)} au {to_fr(d2)}   |   {len(rows)} entrée(s)" + (f"   |   Filtre : {term}" if term else "")],
                colonnes=[("N°", 0.6, 'l', False), ("Date", 1.5, 'l', False), ("Utilisateur", 1.4, 'l', False), ("Action", 2.2, 'l', True), ("Détails", 7, 'l', True)],
                lignes=lignes, resume=[("Entrées imprimées", str(len(rows))), ("Intégrité du journal", "VÉRIFIÉE" if ok else "ALTÉRATION DÉTECTÉE")],
                note=("Contrôle d'intégrité : " + msg), signatures=["Édité par", "Visa du gérant"])
    chemin = pdf_tableau(spec, f"JOURNAL_AUDIT_{d1}_{d2}")
    if journaliser:
        db.journaliser_audit(d1, d2)
    return chemin
