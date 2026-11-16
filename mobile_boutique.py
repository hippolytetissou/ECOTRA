# -*- coding: utf-8 -*-
"""
GESTION DE BOUTIQUE - Accès depuis un téléphone (mobile_boutique.py)

Le poste SERVEUR sert aussi une petite application web pour téléphone : on ouvre l'adresse affichée dans
Administration > Réseau > « Utiliser avec un téléphone » (par exemple http://192.168.1.20:8765/m) dans le navigateur.
Aucune installation sur le téléphone. Fonctions : accueil (chiffres du jour), vente (paiement mixte, client, crédit),
dettes à encaisser, stock, dépenses, factures et reçus en PDF.

Sécurité : code d'accès réseau + identifiant + mot de passe ; mêmes droits que sur l'ordinateur (revérifiés par le serveur) ;
l'identité du vendeur est celle de la session. Les échanges ne sont pas chiffrés : à utiliser sur le réseau de la boutique
(Wi-Fi, point d'accès du téléphone ou partage de connexion Bluetooth), jamais sur Internet.
"""
import hmac
import io
import json
import math
import os
import re
import sqlite3
import time
from urllib.parse import parse_qs, urlparse

import database_boutique as D

_VENTE_ROLES = {'admin', 'gerant', 'vendeur'}


# ---------------------------------------------------------------- utilitaires
def plain(x):
    """Rend n'importe quel résultat de la base (lignes SQL, tuples...) sérialisable en JSON."""
    if isinstance(x, sqlite3.Row):
        return {k: plain(x[k]) for k in x.keys()}
    if isinstance(x, dict):
        return {k: plain(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [plain(v) for v in x]
    if isinstance(x, float) and not math.isfinite(x):
        return 0
    return x


def _err(msg, **kw):
    return dict({'ok': False, 'error': msg}, **kw)


def _num(v, default=0.0):
    try:
        f = float(str(v).replace(' ', '').replace(',', '.'))
    except (TypeError, ValueError):
        return default
    return f if math.isfinite(f) else default


def _session(host, token):
    s = host.sessions.get(token)
    if not s:
        return None
    from reseau_boutique import SESSION_HEURES
    if time.time() - s['last'] > SESSION_HEURES * 3600:
        host.sessions.pop(token, None)
        return None
    s['last'] = time.time()
    return s


# ---------------------------------------------------------------- API JSON (POST /m/api)
def handle_api(host, req, ip):
    act = str(req.get('action', ''))
    if act == 'info':                                                  # page de connexion : nom et modes de paiement
        cfg = D.get_config()
        return {'ok': True, 'boutique': cfg.get('boutique', ''), 'tva_taux': cfg.get('tva_taux', 0),
                'modes': [m for m in D.MODES if m != "Crédit client"]}
    if act == 'login':
        return _login(host, req, ip)
    sess = _session(host, req.get('token'))
    if not sess:
        return _err("Session expirée : reconnectez-vous.", expired=True)
    try:
        fn = _ACTIONS.get(act)
        if not fn:
            return _err(f"Action inconnue : {act}")
        return fn(host, sess, req)
    except PermissionError as e:
        return _err(str(e) or "Action non autorisée pour votre rôle.")
    except (ValueError, sqlite3.Error) as e:
        return _err(f"Opération refusée : {e}")


def _login(host, req, ip):
    if host.blocked(ip):
        return _err("Trop d'essais : patientez une minute.")
    code = str(req.get('code', '')).strip().upper()
    if not hmac.compare_digest(code.encode(), str(host.code).strip().upper().encode()):
        host.fail(ip)
        return _err("Code d'accès réseau incorrect.")
    out = host.handle({'op': 'login', 'username': req.get('username', ''), 'password': req.get('password', '')}, ip)
    if not out.get('user'):
        host.fail(ip)
        return _err(out.get('auth_msg') or "Identifiant ou mot de passe incorrect.")
    host.ok(ip)
    u = plain(out['user'])
    cfg = D.get_config()
    return {'ok': True, 'token': out['token'], 'must_change': bool(u.get('must_change')),
            'user': {'username': u['username'], 'full_name': u.get('full_name') or u['username'], 'role': u['role']},
            'boutique': cfg.get('boutique', ''), 'tva_taux': cfg.get('tva_taux', 0), 'modes': [m for m in D.MODES if m != "Crédit client"]}


def _dashboard(host, sess, req):
    return {'ok': True, 'data': plain(host.invoke(sess, 'tableau_de_bord'))}


def _search(host, sess, req):
    rows = host.invoke(sess, 'search_articles', [str(req.get('q', '')).strip()], {'limit': 40})
    keep = ('id', 'code', 'designation', 'categorie', 'prix_vente', 'stock', 'seuil_alerte')
    return {'ok': True, 'items': [{k: plain(r[k]) for k in keep} for r in rows]}


def _stock(host, sess, req):
    if req.get('only_low'):
        rows = host.invoke(sess, 'alertes_stock')
    else:
        rows = host.invoke(sess, 'search_articles', [str(req.get('q', '')).strip()], {'limit': 60})
    keep = ('id', 'code', 'designation', 'categorie', 'prix_vente', 'stock', 'seuil_alerte')
    return {'ok': True, 'items': [{k: plain(r[k]) for k in keep} for r in rows][:80]}


def _clients(host, sess, req):
    rows = host.invoke(sess, 'get_clients', [str(req.get('q', '')).strip()])
    return {'ok': True, 'items': [{'id': r['id'], 'nom': r['nom'], 'telephone': r['telephone'], 'solde': plain(r['solde'])} for r in rows][:30]}


def _sell(host, sess, req):
    cfg = D.get_config()
    lignes, sous = [], 0
    for l in req.get('lines') or []:
        a = host.invoke(sess, 'get_article', [int(l.get('id', 0))])
        qte, rem = _num(l.get('qty')), _num(l.get('rem'))
        if not a or qte <= 0 or not (0 <= rem <= 100):
            return _err("Une ligne du panier est invalide.")
        lignes.append((a['id'], qte, None, rem))                       # prix catalogue : le téléphone ne peut pas modifier un prix
        sous += round(qte * a['prix_vente'] * (1 - rem / 100))
    if not lignes:
        return _err("Le panier est vide.")
    rg = _num(req.get('remise_globale'))
    if not (0 <= rg <= 100):
        return _err("Remise invalide.")
    remise = round(sous * rg / 100)
    total = sous - remise + round((sous - remise) * _num(cfg.get('tva_taux')) / 100)
    pays = [(str(p.get('mode', '')), _num(p.get('montant')), str(p.get('ref', '')).strip()) for p in req.get('payments') or []]
    regs, rendu, err = D.plan_payments(total, pays)
    if err:
        return _err(err)
    cid = req.get('client_id') or None
    ok, msg, vid = host.invoke(sess, 'vendre', [lignes, regs, cid, rg])
    if not ok:
        return _err(msg)
    v = host.invoke(sess, 'get_vente', [vid])
    return {'ok': True, 'vente_id': vid, 'facture_no': v['facture_no'], 'total': total, 'rendu': rendu, 'reste': max(total - sum(r[1] for r in regs), 0)}


def _debts(host, sess, req):
    rows = host.invoke(sess, 'get_ventes', [], {'term': str(req.get('q', '')).strip(), 'impayees': True, 'limit': 100})
    return {'ok': True, 'items': [{'id': r['id'], 'facture_no': r['facture_no'], 'date': r['date'][:10], 'client': r['client'] or "Client de passage",
                                   'total': r['total'], 'paye': r['paye'], 'reste': round(r['total'] - r['paye'])} for r in rows if not r['annule']]}


def _pay_debt(host, sess, req):
    (ok, msg, rid), attrs = host.invoke(sess, 'encaisser_credit', [int(req.get('vente_id', 0)), _num(req.get('montant')), str(req.get('mode', 'Espèces')),
                                                                  str(req.get('ref', '')).strip()], attrs=True)
    return {'ok': True, 'msg': msg, 'reglement_id': rid} if ok else _err(msg)


def _expense(host, sess, req):
    (ok, msg), attrs = host.invoke(sess, 'add_depense', [str(req.get('libelle', '')), _num(req.get('montant')), str(req.get('categorie', ''))], attrs=True)
    return {'ok': True, 'msg': msg, 'depense_id': attrs.get('last_depense_id')} if ok else _err(msg)


def _change_password(host, sess, req):
    ok, msg = host.invoke(sess, 'change_password', [sess['actor'], str(req.get('old', '')), str(req.get('new', ''))])
    return {'ok': True, 'msg': msg} if ok else _err(msg)


def _me(host, sess, req):
    cfg = D.get_config()
    u = sess['user']
    return {'ok': True, 'user': {'username': u['username'], 'full_name': u.get('full_name') or u['username'], 'role': u['role']},
            'boutique': cfg.get('boutique', ''), 'tva_taux': cfg.get('tva_taux', 0), 'modes': [x for x in D.MODES if x != "Crédit client"]}


def _logout(host, sess, req):
    host.sessions.pop(req.get('token'), None)
    return {'ok': True}


_ACTIONS = {'me': _me, 'dashboard': _dashboard, 'search': _search, 'stock': _stock, 'clients': _clients, 'sell': _sell, 'debts': _debts,
            'pay_debt': _pay_debt, 'expense': _expense, 'change_password': _change_password, 'logout': _logout}


# ---------------------------------------------------------------- application installable (PWA)
_ICONES = {}


def _logo_fichier():
    p = (D.get_config().get('logo') or '').strip()
    return p if p and os.path.isfile(p) else ''


def pwa_possible():
    try:
        import PIL  # noqa: F401
        return True
    except ImportError:
        return False


def icone_png(taille, mode='any'):
    """Icône de l'application à partir du LOGO de l'entreprise (sinon l'initiale de la boutique).
    mode : 'any' (carré blanc arrondi, fond transparent) | 'maskable' (pleine surface, zone de sécurité Android) | 'apple' (opaque, iPhone).
    Retourne les octets PNG, ou None si Pillow n'est pas installé."""
    if not pwa_possible():
        return None
    from PIL import Image, ImageDraw, ImageFont
    logo = _logo_fichier()
    nom = D.get_config().get('boutique', '') or 'B'
    cle = (taille, mode, logo, os.path.getmtime(logo) if logo else 0, nom)
    if cle in _ICONES:
        return _ICONES[cle]
    t = taille
    plein = mode in ('maskable', 'apple')
    marge = 0.22 if mode == 'maskable' else 0.14
    img = Image.new('RGBA', (t, t), (255, 255, 255, 255) if plein else (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    if not plein:
        d.rounded_rectangle((0, 0, t - 1, t - 1), radius=int(t * 0.2), fill=(255, 255, 255, 255))
    zone = int(t * (1 - 2 * marge))
    dessine = False
    if logo:
        try:
            with Image.open(logo) as src:
                lg = src.convert('RGBA')
            lg.thumbnail((zone, zone))
            img.alpha_composite(lg, ((t - lg.width) // 2, (t - lg.height) // 2))
            dessine = True
        except Exception:
            dessine = False
    if not dessine:                                            # pas de logo : pastille bleue avec l'initiale de la boutique
        d.rounded_rectangle((int(t * marge), int(t * marge), t - int(t * marge), t - int(t * marge)), radius=int(t * 0.16), fill=(15, 42, 82, 255))
        lettre = next((c for c in nom if c.isalnum()), 'B').upper()
        police = None
        for nom_police in ('arialbd.ttf', 'DejaVuSans-Bold.ttf', 'Arial Bold.ttf'):
            try:
                police = ImageFont.truetype(nom_police, int(t * 0.42)); break
            except OSError:
                continue
        if police is None:
            try:
                police = ImageFont.load_default(size=int(t * 0.42))
            except TypeError:
                police = ImageFont.load_default()
        d.text((t / 2, t / 2), lettre, fill=(255, 255, 255, 255), font=police, anchor='mm')
    sortie = io.BytesIO()
    (img.convert('RGB') if mode == 'apple' else img).save(sortie, 'PNG', optimize=True)
    _ICONES[cle] = sortie.getvalue()
    return _ICONES[cle]


def icone_svg():
    nom = D.get_config().get('boutique', '') or 'B'
    lettre = next((c for c in nom if c.isalnum()), 'B').upper()
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512"><rect width="512" height="512" rx="100" fill="#0f2a52"/>'
            f'<text x="256" y="350" font-size="300" font-family="Arial,sans-serif" font-weight="700" text-anchor="middle" fill="#fff">{lettre}</text></svg>').encode()


def manifeste():
    cfg = D.get_config()
    nom = cfg.get('boutique', '') or 'Boutique'
    icones = ([{'src': '/m/icon-192.png', 'sizes': '192x192', 'type': 'image/png', 'purpose': 'any'},
               {'src': '/m/icon-512.png', 'sizes': '512x512', 'type': 'image/png', 'purpose': 'any'},
               {'src': '/m/icon-maskable-512.png', 'sizes': '512x512', 'type': 'image/png', 'purpose': 'maskable'}]
              if pwa_possible() else [{'src': '/m/icon.svg', 'sizes': 'any', 'type': 'image/svg+xml', 'purpose': 'any'}])
    return json.dumps({
        'id': '/m/', 'name': nom, 'short_name': nom[:12], 'description': "Gestion de la boutique : ventes, dettes, stock, dépenses.",
        'start_url': '/m/', 'scope': '/m/', 'display': 'standalone', 'orientation': 'portrait', 'lang': 'fr',
        'background_color': '#0f2a52', 'theme_color': '#0f2a52', 'icons': icones,
        'shortcuts': [{'name': 'Vendre', 'url': '/m/#sell'}, {'name': 'Dettes à encaisser', 'url': '/m/#debts'}, {'name': 'Stock', 'url': '/m/#stock'}]},
        ensure_ascii=False).encode('utf-8')


def version_cache():
    import hashlib
    lg = _logo_fichier()
    return hashlib.sha1((PAGE + (D.get_config().get('boutique', '')) + (str(os.path.getmtime(lg)) if lg else '')).encode('utf-8')).hexdigest()[:10]


SERVICE_WORKER = r"""
const V='gb-__V__';
self.addEventListener('install',e=>{e.waitUntil(caches.open(V).then(c=>c.add('/m/')).then(()=>self.skipWaiting()))});
self.addEventListener('activate',e=>{e.waitUntil(caches.keys().then(ks=>Promise.all(ks.filter(k=>k!==V).map(k=>caches.delete(k)))).then(()=>self.clients.claim()))});
self.addEventListener('fetch',e=>{
  const u=new URL(e.request.url);
  if(e.request.method!=='GET'||u.origin!==location.origin)return;
  const p=u.pathname;
  /* la page et ses icônes : réseau d'abord (toujours à jour), copie locale si l'ordinateur est injoignable. L'API et les PDF ne sont JAMAIS mis en cache. */
  if(p==='/m'||p==='/m/'||p==='/m/manifest.webmanifest'||/^\/m\/(icon|apple-touch|logo)/.test(p)){
    e.respondWith(fetch(e.request).then(r=>{if(r.ok){const cp=r.clone();caches.open(V).then(c=>c.put(e.request,cp));return r}
      /* ordinateur éteint : Cloudflare répond par une page d'erreur (530/1033) : on garde la copie locale de l'application */
      return caches.match(e.request).then(c=>c||r)}).catch(()=>caches.match(e.request).then(r=>r||caches.match('/m/'))));
  }
});
"""


# ---------------------------------------------------------------- pages et PDF (GET)
def handle_get(host, path):
    """Retourne (statut, type de contenu, octets)."""
    u = urlparse(path)
    q = {k: v[0] for k, v in parse_qs(u.query).items()}
    if u.path in ('/m', '/m/'):
        return 200, 'text/html; charset=utf-8', PAGE.encode('utf-8')
    if u.path == '/m/manifest.webmanifest':
        return 200, 'application/manifest+json; charset=utf-8', manifeste()
    if u.path == '/m/sw.js':
        return 200, 'application/javascript; charset=utf-8', SERVICE_WORKER.replace('__V__', version_cache()).encode('utf-8')
    icones = {'/m/icon-192.png': (192, 'any'), '/m/icon-512.png': (512, 'any'), '/m/icon-maskable-512.png': (512, 'maskable'), '/m/apple-touch-icon.png': (180, 'apple')}
    if u.path in icones:
        data = icone_png(*icones[u.path])
        return (200, 'image/png', data) if data else (404, 'text/plain; charset=utf-8', b"Pillow non installe.")
    if u.path == '/m/icon.svg':
        return 200, 'image/svg+xml', icone_svg()
    if u.path == '/m/logo.png':
        lg = _logo_fichier()
        if not lg:
            return 404, 'text/plain; charset=utf-8', b"Pas de logo."
        with open(lg, 'rb') as f:
            return 200, 'image/png' if lg.lower().endswith('.png') else 'image/jpeg', f.read()
    if u.path == '/m/doc':
        sess = _session(host, q.get('t'))
        if not sess or sess['role'] not in _VENTE_ROLES:
            return 403, 'text/plain; charset=utf-8', "Session expirée ou droits insuffisants.".encode()
        try:
            import facture_boutique as F
            fmt = q.get('fmt') if q.get('fmt') in ('ticket', 'a4') else 'ticket'
            maker = {'facture': F.make_ticket, 'recu': F.recu_reglement, 'depense': F.bon_depense}.get(q.get('kind'))
            if not maker:
                return 404, 'text/plain; charset=utf-8', b"Document inconnu."
            with host.lock:
                chemin = maker(host.get_db(), int(q.get('id', 0)), format=fmt)
            with open(chemin, 'rb') as f:
                data = f.read()
            return 200, ('application/pdf' if chemin.endswith('.pdf') else 'text/plain; charset=utf-8'), data
        except Exception as e:                                          # noqa
            return 500, 'text/plain; charset=utf-8', f"Document impossible : {e}".encode()
    return 404, 'text/plain; charset=utf-8', b"Page introuvable."


# ---------------------------------------------------------------- aides à la connexion du téléphone
RULE_NAME = "Gestion Boutique Telephone"


def qr_matrix(text):
    """QR code sous forme de grille de booléens (pour l'afficher à l'écran). Nécessite reportlab."""
    from reportlab.graphics.barcode import qrencoder as Q
    q = Q.QRCode(None, Q.QRErrorCorrectLevel.M)
    q.addData(text)
    q.make()
    n = q.getModuleCount()
    return [[bool(q.isDark(r, c)) for c in range(n)] for r in range(n)]


def _info_ip(ip):
    """(rang, explication) d'une adresse. Rang 0 = la plus fiable pour un téléphone, 4 = celle qui marche rarement.
    Quand le TÉLÉPHONE partage lui-même sa connexion (point d'accès ou Bluetooth), il ne peut en général pas ouvrir une page
    d'un appareil connecté à lui : c'est une limite d'Android / iPhone, pas de l'application."""
    if ip.startswith('192.168.137.'):
        return 0, "point d'accès de l'ordinateur (RECOMMANDÉ sans box)"
    parts = ip.split('.')
    if parts[0] == '100' and len(parts) == 4 and parts[1].isdigit() and 64 <= int(parts[1]) <= 127:
        return 2, "Tailscale (réseau privé sécurisé : marche partout, même en 4G)"
    if ip.startswith('192.168.44.'):
        return 4, "partage de connexion Bluetooth (le téléphone ne peut généralement pas l'utiliser)"
    if ip.startswith(('192.168.43.', '172.20.10.', '192.168.49.')):
        return 4, "point d'accès du téléphone (le téléphone ne peut généralement pas l'utiliser)"
    if ip.startswith('192.168.') or ip.startswith('10.') or any(ip.startswith(f'172.{k}.') for k in range(16, 32)):
        return 1, "Wi-Fi / box de la boutique"
    if ip.startswith('169.254.'):
        return 9, "adresse inutilisable (pas de réseau)"
    return 5, "autre réseau"


def classer_ips(ips):
    """Adresses de l'ordinateur, la plus probable en premier, avec une explication simple de chaque réseau."""
    return [(ip, _info_ip(ip)[1]) for ip in sorted(set(ips), key=lambda i: (_info_ip(i)[0], i)) if _info_ip(ip)[0] != 9]


_VIRTUEL = re.compile(r'vethernet|hyper-v|wsl|virtualbox|vmware|docker|vpn|tap-|tunnel|loopback|miniport|host-only|npcap|teredo|isatap|pseudo', re.I)


def separer_interfaces(rows):
    """rows : [{'Alias','Desc','IP','GW'}] décrivant les cartes réseau de l'ordinateur.
    Retourne (utilisables, ignorees) : les cartes réelles (avec passerelle d'abord) et les réseaux VIRTUELS de Windows
    (Hyper-V, WSL, Docker, VMware, VPN...) dont l'adresse est inutilisable depuis un téléphone."""
    ok, ignore = [], []
    for r in rows:
        ip = r.get('IP')
        if not ip or ip.startswith(('127.', '169.254.')):
            continue
        nom = (r.get('Alias') or '').strip()
        texte = f"{r.get('Alias') or ''} {r.get('Desc') or ''}"
        if _VIRTUEL.search(texte) and not re.search(r'tailscale', texte, re.I):
            ignore.append((ip, f"{nom} : réseau virtuel de l'ordinateur (inutilisable depuis un téléphone)"))
            continue
        genre = ("Tailscale" if re.search(r'tailscale', texte, re.I) else "Bluetooth" if re.search(r'bluetooth', texte, re.I) else "Wi-Fi" if re.search(r'wi-?fi|wlan|sans fil|wireless', texte, re.I)
                 else "câble réseau" if re.search(r'ethernet', texte, re.I) else "")
        indice = _info_ip(ip)[1]
        ok.append((not r.get('GW'), ip, " - ".join(x for x in (nom or genre, indice) if x)))
    ok.sort(key=lambda x: (_info_ip(x[1])[0], x[0], x[1]))
    return [(ip, lab) for _, ip, lab in ok], ignore


def detecter_reseaux():
    """(utilisables, ignorees) : adresses à donner au téléphone, et adresses virtuelles à NE PAS utiliser.
    Sous Windows, interroge les cartes réseau (nom + passerelle) pour écarter les réseaux virtuels (Hyper-V, WSL...)."""
    import json as _json
    import subprocess
    import reseau_boutique as N
    if os.name == 'nt':
        try:
            ps = ("[Console]::OutputEncoding=[Text.Encoding]::UTF8; Get-NetIPConfiguration | ForEach-Object { [pscustomobject]@{Alias=$_.InterfaceAlias; "
                  "Desc=$_.InterfaceDescription; IP=($_.IPv4Address | Select-Object -First 1 -ExpandProperty IPAddress); "
                  "GW=($_.IPv4DefaultGateway | Select-Object -First 1 -ExpandProperty NextHop)} } | ConvertTo-Json -Compress")
            r = subprocess.run(['powershell', '-NoProfile', '-NonInteractive', '-Command', ps], capture_output=True, timeout=15,
                               creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            data = _json.loads(r.stdout.decode('utf-8', 'replace').strip() or '[]')
            rows = [data] if isinstance(data, dict) else data
            ok, ignore = separer_interfaces(rows)
            if ok or ignore:
                return ok, ignore
        except (OSError, ValueError, subprocess.SubprocessError):
            pass
    ips = N.lan_ips()                                  # repli : sans nom de carte, on écarte les adresses de type 172.x.x.1 (réseaux virtuels)
    virt = [i for i in ips if re.match(r'^172\.(1[6-9]|2\d|3[01])\.\d+\.1$', i) and not i.startswith('172.20.10.')]
    return classer_ips([i for i in ips if i not in virt]), [(i, "réseau virtuel probable (inutilisable depuis un téléphone)") for i in virt]


def tester_acces(port, ips):
    """Teste le serveur depuis cet ordinateur : [(libellé, ok, explication)]."""
    import http.client
    import socket
    res = []
    try:
        h = http.client.HTTPConnection('127.0.0.1', port, timeout=3)
        h.request('GET', '/m')
        r = h.getresponse()
        corps = r.read()
        ok = r.status == 200 and b'Gestion boutique' in corps
        res.append(("Serveur de l'application", ok, "répond correctement" if ok else f"réponse inattendue (code {r.status})"))
    except (OSError, http.client.HTTPException) as e:
        res.append(("Serveur de l'application", False, f"ne répond pas ({e}). Le mode Serveur est-il activé ?"))
    for ip in ips:
        try:
            with socket.create_connection((ip, port), timeout=3):
                res.append((f"Adresse {ip}", True, "joignable depuis cet ordinateur"))
        except OSError as e:
            res.append((f"Adresse {ip}", False, f"injoignable ({e})"))
    return res


def pare_feu_etat(port):
    """True / False si la règle de pare-feu Windows existe ; None si on ne peut pas le savoir (autre système)."""
    if os.name != 'nt':
        return None
    import subprocess
    try:
        r = subprocess.run(['netsh', 'advfirewall', 'firewall', 'show', 'rule', f'name={RULE_NAME}'], capture_output=True, text=True,
                           creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0), timeout=10)
        return r.returncode == 0 and str(port) in r.stdout
    except (OSError, subprocess.SubprocessError):
        return None


def pare_feu_autoriser(port):
    """Ajoute la règle de pare-feu Windows (une demande d'autorisation « Oui » apparaît). Retourne True si la demande est lancée."""
    if os.name != 'nt':
        return False
    try:
        import ctypes
        args = (f'advfirewall firewall add rule name="{RULE_NAME}" dir=in action=allow protocol=TCP localport={port} profile=private,public')
        return ctypes.windll.shell32.ShellExecuteW(None, "runas", "netsh", args, None, 0) > 32
    except (OSError, AttributeError):
        return False


# ---------------------------------------------------------------- affiche QR à imprimer
def affiche_qr(urls, chemin, internet=False):
    """PDF A4 : QR code de l'adresse à scanner avec le téléphone + mode d'emploi. Retourne le chemin ou None sans reportlab."""
    try:
        from reportlab.pdfgen import canvas
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.units import mm
        from reportlab.lib.colors import HexColor, white
        from reportlab.graphics.barcode import qr
        from reportlab.graphics import renderPDF
        from reportlab.graphics.shapes import Drawing
    except ImportError:
        return None
    W, H = A4
    c = canvas.Canvas(chemin, pagesize=A4)
    cfg = D.get_config()
    c.setFillColor(HexColor('#0f2a52')); c.rect(0, H - 40 * mm, W, 40 * mm, stroke=0, fill=1)
    c.setFillColor(white); c.setFont('Helvetica-Bold', 22); c.drawCentredString(W / 2, H - 20 * mm, cfg.get('boutique', '')[:40])
    c.setFont('Helvetica', 13); c.drawCentredString(W / 2, H - 30 * mm, "Gestion de la boutique sur téléphone")
    url = urls[0]
    w = qr.QrCodeWidget(url); b = w.getBounds(); t = 95 * mm
    d = Drawing(t, t, transform=[t / (b[2] - b[0]), 0, 0, t / (b[3] - b[1]), 0, 0]); d.add(w)
    renderPDF.draw(d, c, (W - t) / 2, H - 150 * mm)
    c.setFillColor(HexColor('#1f2933')); c.setFont('Helvetica-Bold', 16); c.drawCentredString(W / 2, H - 160 * mm, url)
    c.setFont('Helvetica', 11)
    y = H - 178 * mm
    etape1 = (("1. Aucun réglage : le téléphone a seulement besoin d'Internet (4G ou Wi-Fi).", "    L'ordinateur de la caisse doit rester allumé avec l'application ouverte.")
              if internet else ("1. Connectez le téléphone au même Wi-Fi que l'ordinateur de la caisse", "    (ou au point d'accès / partage de connexion Bluetooth, selon la configuration)."))
    for ligne in (etape1[0], etape1[1],
                  "2. Scannez ce code avec l'appareil photo, ou tapez l'adresse ci-dessus dans le navigateur.",
                  "3. Connectez-vous avec votre identifiant, votre mot de passe et le code d'accès réseau",
                  "    (demandez-le au gérant : il n'est pas imprimé ici pour des raisons de sécurité).",
                  "4. Astuce : ajoutez la page à l'écran d'accueil du téléphone pour la retrouver en un geste."):
        c.drawString(25 * mm, y, ligne); y -= 7 * mm
    if len(urls) > 1:
        c.setFont('Helvetica', 10); c.setFillColor(HexColor('#64748b'))
        c.drawString(25 * mm, y - 4 * mm, "Autres adresses possibles : " + "   ".join(urls[1:]))
    c.showPage(); c.save()
    return chemin


# ---------------------------------------------------------------- la page web du téléphone
PAGE = r"""<!DOCTYPE html>
<html lang="fr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#0f2a52"><meta name="apple-mobile-web-app-capable" content="yes"><meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent"><meta name="apple-mobile-web-app-title" content="Boutique">
<link rel="manifest" href="/m/manifest.webmanifest"><link rel="apple-touch-icon" href="/m/apple-touch-icon.png"><link rel="icon" href="/m/icon-192.png">
<title>Gestion boutique</title>
<style>
:root{--bg:#f3f5f9;--card:#fff;--fg:#1f2933;--muted:#64748b;--line:#d9e0ea;--head:#0f2a52;--acc:#2563eb;--ok:#16a34a;--bad:#dc2626;--warn:#ea580c;--soft:#e5eaf2}
@media (prefers-color-scheme:dark){:root{--bg:#0b1220;--card:#131c2e;--fg:#e6edf7;--muted:#93a4bd;--line:#26334b;--soft:#1d2940}}
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
body{margin:0;background:var(--bg);color:var(--fg);font:16px/1.35 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;padding-bottom:calc(76px + env(safe-area-inset-bottom))}
header{position:sticky;top:0;z-index:5;background:var(--head);color:#fff;padding:calc(10px + env(safe-area-inset-top)) 14px 10px;display:flex;justify-content:space-between;align-items:center}
header b{font-size:17px} header small{opacity:.8}
main{padding:12px;max-width:640px;margin:0 auto}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:12px;margin-bottom:12px}
h2{margin:4px 0 10px;font-size:18px;color:var(--head)} @media (prefers-color-scheme:dark){h2{color:#9cc0ff}}
input,select{width:100%;padding:13px;border:1px solid var(--line);border-radius:12px;font:inherit;background:var(--card);color:var(--fg);margin:4px 0}
input:focus,select:focus{outline:2px solid var(--acc)}
label{font-size:13px;color:var(--muted)}
button{border:0;border-radius:12px;padding:13px 16px;font:inherit;font-weight:700;background:var(--soft);color:var(--fg);min-height:48px;cursor:pointer}
button.p{background:var(--acc);color:#fff} button.ok{background:var(--ok);color:#fff;width:100%;font-size:18px;min-height:54px} button.bad{background:var(--bad);color:#fff}
button.s{min-height:38px;padding:6px 12px;font-size:14px} button:disabled{opacity:.5}
.row{display:flex;gap:8px;align-items:center} .row>*{flex:1} .row>.fit{flex:none}
.item{display:flex;justify-content:space-between;gap:10px;padding:11px 4px;border-bottom:1px solid var(--line);align-items:center}
.item:last-child{border:0} .item .t{font-weight:600} .item .m{font-size:13px;color:var(--muted)}
.badge{display:inline-block;padding:2px 9px;border-radius:99px;font-size:13px;font-weight:700;background:var(--soft)}
.b-ok{background:#dcfce7;color:#166534}.b-low{background:#ffedd5;color:#9a3412}.b-out{background:#fee2e2;color:#991b1b}
.kpis{display:grid;grid-template-columns:1fr 1fr;gap:10px}
.kpi{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:12px;border-left:5px solid var(--acc)}
.kpi small{color:var(--muted);font-size:11px;font-weight:700;letter-spacing:.04em;text-transform:uppercase}.kpi div{font-size:19px;font-weight:800;margin-top:2px}
.total{background:var(--head);color:#fff;border-radius:14px;padding:12px 16px;margin-bottom:12px}.total small{opacity:.8;font-weight:700}.total div{font-size:30px;font-weight:800}
nav{position:fixed;bottom:0;left:0;right:0;background:var(--card);border-top:1px solid var(--line);display:flex;padding-bottom:env(safe-area-inset-bottom);z-index:6}
nav button{flex:1;border-radius:0;background:none;font-size:12px;padding:8px 2px;min-height:62px;color:var(--muted)} nav button.on{color:var(--acc)} nav i{display:block;font-style:normal;font-size:22px}
.chip{display:inline-flex;align-items:center;gap:8px;background:var(--soft);border-radius:99px;padding:6px 12px;margin:2px 0}
.msg{padding:10px 12px;border-radius:12px;margin:8px 0;font-weight:600}.msg.e{background:#fee2e2;color:#991b1b}.msg.k{background:#dcfce7;color:#166534}
.qty{display:flex;align-items:center;gap:8px}.qty button{min-height:38px;width:38px;padding:0;font-size:20px}
.sheet{position:fixed;inset:0;background:rgba(0,0,0,.5);z-index:10;display:flex;align-items:flex-end}.sheet>div{background:var(--card);width:100%;max-width:640px;margin:0 auto;border-radius:18px 18px 0 0;padding:16px;padding-bottom:calc(16px + env(safe-area-inset-bottom));max-height:90vh;overflow:auto}
.hide{display:none}.muted{color:var(--muted)}.center{text-align:center}
</style></head><body>
<header><div style="display:flex;align-items:center;gap:10px"><img id="logo" alt="" class="hide" src="/m/logo.png" onload="this.classList.remove('hide')" onerror="this.remove()" style="height:36px;max-width:60px;object-fit:contain;border-radius:8px;background:#fff;padding:2px"><div><b id="shop">Gestion boutique</b><br><small id="who"></small></div></div><button class="s hide" id="bout" onclick="logout()">Quitter</button></header>
<main id="v"></main>
<nav id="nav" class="hide">
<button data-t="home" onclick="go('home')"><i>&#127968;</i>Accueil</button>
<button data-t="sell" onclick="go('sell')"><i>&#128722;</i>Vendre</button>
<button data-t="debts" onclick="go('debts')"><i>&#128181;</i>Dettes</button>
<button data-t="stock" onclick="go('stock')"><i>&#128230;</i>Stock</button>
<button data-t="more" onclick="go('more')"><i>&#9776;</i>Plus</button>
</nav>
<script>
var S={token:localStorage.getItem('gb_token')||'',user:null,tva:0,modes:['Espèces'],cart:[],pays:[],client:null,rg:0,tab:'home'};
var $=function(i){return document.getElementById(i)};
function esc(s){return String(s==null?'':s).replace(/[&<>"']/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]})}
function fmt(n){return Math.round(+n||0).toString().replace(/\B(?=(\d{3})+(?!\d))/g,'\u202f')+' FCFA'}
function num(v){var f=parseFloat(String(v).replace(/\s/g,'').replace(',','.'));return isFinite(f)?f:0}
function api(action,data){
  var body=Object.assign({action:action,token:S.token},data||{});
  return fetch('/m/api',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)})
   .then(function(r){return r.json()}).then(function(j){if(j.expired){S.token='';localStorage.removeItem('gb_token');showLogin(j.error)}return j})
   .catch(function(){return {ok:false,error:"Connexion impossible avec l\\u2019ordinateur de la caisse. Vérifiez qu\\u2019il est allumé, que l\\u2019application est ouverte et qu\\u2019Internet fonctionne."}});
}
function msg(t,k){return '<div class="msg '+(k?'k':'e')+'">'+esc(t)+'</div>'}
function debounce(f,ms){var t;return function(){var a=arguments;clearTimeout(t);t=setTimeout(function(){f.apply(null,a)},ms)}}
/* ---------- connexion ---------- */
function showLogin(note){
  S.user=null;$('nav').classList.add('hide');$('bout').classList.add('hide');$('who').textContent='';
  $('v').innerHTML='<div class="card"><h2>Connexion</h2>'+(note?msg(note):'')+
   '<label>Identifiant</label><input id="lu" autocapitalize="none" autocomplete="username" value="'+esc(localStorage.getItem('gb_user')||'')+'">'+
   '<label>Mot de passe</label><input id="lp" type="password" autocomplete="current-password">'+
   '<label>Code d\u2019accès réseau (donné par le gérant)</label><input id="lc" autocapitalize="characters" autocomplete="off" value="'+esc(localStorage.getItem('gb_code')||'')+'">'+
   '<div id="le"></div><button class="p" style="width:100%;margin-top:8px" onclick="doLogin()">Se connecter</button></div>';
  api('info').then(function(j){if(j.ok){setNom(j.boutique)}});
}
function doLogin(){
  var u=$('lu').value.trim(),p=$('lp').value,c=$('lc').value.trim();
  if(!u||!p||!c){$('le').innerHTML=msg('Remplissez les trois champs.');return}
  $('le').innerHTML='<div class="muted">Connexion…</div>';
  api('login',{username:u,password:p,code:c}).then(function(j){
    if(!j.ok){$('le').innerHTML=msg(j.error);return}
    S.token=j.token;S.user=j.user;S.tva=j.tva_taux||0;S.modes=j.modes;
    localStorage.setItem('gb_token',j.token);localStorage.setItem('gb_user',u);localStorage.setItem('gb_code',c);
    setNom(j.boutique);ready(j.must_change);
  });
}
function ready(must){
  $('who').textContent=S.user.full_name+' · '+S.user.role;$('nav').classList.remove('hide');$('bout').classList.remove('hide');
  if(must){go('more',"Votre mot de passe a été réinitialisé : choisissez-en un nouveau.");return}
  var h=location.hash.replace('#','');go(['sell','debts','stock','more','home'].indexOf(h)>=0?h:'home');
}
function setNom(n){n=n||'Gestion boutique';$('shop').textContent=n;document.title=n;var t=document.querySelector('meta[name=apple-mobile-web-app-title]');if(t)t.content=n.slice(0,14)}
function logout(){api('logout').then(function(){S.token='';localStorage.removeItem('gb_token');S.cart=[];S.pays=[];S.client=null;showLogin()})}
function go(t,note){
  S.tab=t;Array.prototype.forEach.call(document.querySelectorAll('nav button'),function(b){b.classList.toggle('on',b.dataset.t===t)});
  ({home:home,sell:sell,debts:debts,stock:stock,more:more})[t](note);window.scrollTo(0,0);
}
/* ---------- accueil ---------- */
function kpi(t,v,c){return '<div class="kpi" style="border-left-color:'+(c||'var(--acc)')+'"><small>'+esc(t)+'</small><div>'+esc(v)+'</div></div>'}
function home(){
  $('v').innerHTML='<div class="muted center">Chargement…</div>';
  api('dashboard').then(function(j){
    if(!j.ok){$('v').innerHTML=msg(j.error);return}
    var d=j.data,k='';
    if(d.marge_jour!==undefined){
      k=kpi('Ventes du jour',fmt(d.ca_jour))+kpi('Marge du jour',fmt(d.marge_jour),'var(--ok)')+kpi('Chiffre du mois',fmt(d.ca_mois))+kpi('Créances clients',fmt(d.creances),'var(--warn)')+
        kpi('Dettes fournisseurs',fmt(d.dettes_fournisseurs),'var(--warn)')+kpi('Valeur du stock',fmt(d.valeur_stock),'#7c3aed');
    }else if(d.ca_jour!==undefined){
      k=kpi('Mes ventes du jour',fmt(d.ca_jour))+kpi('Espèces encaissées',fmt(d.especes_jour),'var(--ok)')+kpi('Factures impayées',d.impayees,'var(--warn)')+kpi('Ruptures',d.ruptures,'var(--bad)');
    }else{k=kpi('Ruptures',d.ruptures,'var(--bad)')+kpi('Stock bas',d.stock_bas,'var(--warn)')}
    var h=(S.inst&&!standalone()?installCard():'')+'<div class="kpis">'+k+'</div>',w=[];
    if(d.ruptures)w.push(d.ruptures+' article(s) en rupture de stock');
    if(d.stock_bas)w.push(d.stock_bas+' article(s) sous le seuil d\u2019alerte');
    if(d.impayees)w.push(d.impayees+' facture(s) avec un reste à payer');
    if(d.ventes_jour&&d.caisse_cloturee===false&&new Date().getHours()>=17)w.push('La caisse du jour n\u2019est pas encore clôturée');
    h+='<div class="card" style="margin-top:12px"><h2>À surveiller</h2>'+(w.length?w.map(function(x){return '<div class="item">'+esc(x)+'</div>'}).join(''):'<div class="muted">Tout est en ordre.</div>')+'</div>';
    if(d.sept_jours){
      var mx=Math.max.apply(null,d.sept_jours.map(function(x){return x[1]}))||1;
      h+='<div class="card"><h2>7 derniers jours</h2><div style="display:flex;gap:6px;align-items:flex-end;height:110px">'+d.sept_jours.map(function(x){
        return '<div style="flex:1;text-align:center;font-size:11px"><div style="background:var(--acc);border-radius:6px 6px 0 0;height:'+Math.max(3,Math.round(80*x[1]/mx))+'px"></div>'+x[0].slice(8)+'</div>'}).join('')+'</div></div>';
    }
    $('v').innerHTML=h;
  });
}
/* ---------- vente ---------- */
function totals(){
  var sous=0;S.cart.forEach(function(l){sous+=Math.round(l.qty*l.pu*(1-l.rem/100))});
  var rem=Math.round(sous*Math.min(Math.max(S.rg,0),100)/100),tva=Math.round((sous-rem)*S.tva/100);
  return {sous:sous,total:sous-rem+tva};
}
function paidList(){return S.pays.reduce(function(a,p){return a+p.montant},0)}
function sell(note){
  var h='<div class="card"><h2>Rechercher un article</h2><input id="q" placeholder="Nom, code ou ISBN…" autocomplete="off" oninput="searchSell()"><div id="res"></div></div>'+
   '<div id="cart"></div><div id="pay"></div>';
  $('v').innerHTML=(note||'')+h;renderCart();searchSell();
}
var searchSell=debounce(function(){
  var q=$('q')?$('q').value:'';
  api('search',{q:q}).then(function(j){
    if(!$('res'))return;
    $('res').innerHTML=!j.ok?msg(j.error):j.items.map(function(a){
      return '<div class="item" onclick="addItem('+a.id+')"><div><div class="t">'+esc(a.designation)+'</div><div class="m">'+esc(a.categorie)+' · '+fmt(a.prix_vente)+'</div></div>'+stockBadge(a)+'</div>'}).join('')||'<div class="muted">Aucun résultat.</div>';
    window._items={};(j.items||[]).forEach(function(a){window._items[a.id]=a});
  });
},250);
function stockBadge(a){var c=a.stock<=0?'b-out':(a.stock<=a.seuil_alerte?'b-low':'b-ok');return '<span class="badge '+c+'">'+(+a.stock)+'</span>'}
function addItem(id){
  var a=(window._items||{})[id];if(!a)return;
  if(a.stock<=0){alert('Cet article est en rupture de stock.');return}
  var l=S.cart.filter(function(x){return x.id===id})[0];
  if(l){if(l.qty<a.stock)l.qty++;else alert('Stock disponible : '+a.stock)}else S.cart.push({id:id,des:a.designation,pu:a.prix_vente,qty:1,rem:0,stock:a.stock});
  renderCart();
}
function qty(i,d){var l=S.cart[i];l.qty=Math.max(0,Math.min(l.stock,l.qty+d));if(l.qty===0)S.cart.splice(i,1);renderCart()}
function renderCart(){
  if(!$('cart'))return;
  var t=totals().total;
  var h='<div class="total"><small>TOTAL À PAYER</small><div>'+fmt(t)+'</div></div>';
  if(S.cart.length){
    h+='<div class="card"><h2>Panier</h2>'+S.cart.map(function(l,i){return '<div class="item"><div><div class="t">'+esc(l.des)+'</div><div class="m">'+fmt(l.pu)+' × '+l.qty+' = '+fmt(Math.round(l.qty*l.pu))+'</div></div>'+
      '<div class="qty"><button onclick="qty('+i+',-1)">−</button><b>'+l.qty+'</b><button onclick="qty('+i+',1)">+</button></div></div>'}).join('')+'</div>';
  }
  $('cart').innerHTML=h;renderPay();
}
function renderPay(){
  if(!$('pay'))return;
  if(!S.cart.length){$('pay').innerHTML='';return}
  var t=totals().total,rest=Math.max(t-paidList(),0);
  var h='<div class="card"><h2>Client et paiement</h2>';
  h+=S.client?'<div class="chip">'+esc(S.client.nom)+' <button class="s bad" onclick="setClient(null)">✕</button></div>':
     '<label>Client (obligatoire si paiement incomplet)</label><input id="cq" placeholder="Rechercher un client…" oninput="searchClient()"><div id="cres"></div>';
  h+='<div class="row"><div><label>Remise globale %</label><input id="rg" inputmode="decimal" value="'+(S.rg||'')+'" placeholder="0" oninput="S.rg=num(this.value);renderTotalOnly()"></div></div>';
  h+='<label>Mode de paiement</label><select id="pm" onchange="refToggle()">'+S.modes.map(function(m){return '<option>'+esc(m)+'</option>'}).join('')+'</select>';
  h+='<div id="refbox" class="hide"><label>Référence de la transaction</label><input id="pr" autocomplete="off"></div>';
  h+='<label>Montant</label><div class="row"><input id="pa" inputmode="decimal" placeholder="'+Math.round(rest)+'"><button class="fit s" onclick="addPay()">+ Ajouter</button></div>';
  h+=S.pays.map(function(p,i){return '<div class="item"><div>'+esc(p.mode)+(p.ref?' ('+esc(p.ref)+')':'')+'</div><div><b>'+fmt(p.montant)+'</b> <button class="s" onclick="S.pays.splice('+i+',1);renderPay()">✕</button></div></div>'}).join('');
  h+='<div id="note" style="font-weight:700;margin:8px 0"></div><div id="err"></div><button class="ok" onclick="validate()">✓ ENCAISSER</button></div>';
  $('pay').innerHTML=h;
  $('pa').addEventListener('input',payNote);refToggle();payNote();
}
function renderTotalOnly(){var t=totals().total;var e=document.querySelector('.total div');if(e)e.textContent=fmt(t);payNote()}
function refToggle(){var m=$('pm');if(!m)return;$('refbox').classList.toggle('hide',m.value==='Espèces')}
function current(){var t=totals().total,v=$('pa')?$('pa').value.trim():'';return v?num(v):Math.max(t-paidList(),0)}
function payNote(){
  var e=$('note');if(!e)return;var t=totals().total,s=paidList()+current();
  if(s>=t-0.5){e.style.color='var(--ok)';e.textContent=s-t>0.5?'Monnaie à rendre : '+fmt(s-t):'Montant exact'}
  else{e.style.color='var(--warn)';e.textContent='Reste à crédit : '+fmt(t-s)}
}
function addPay(){
  var m=$('pm').value,r=$('pr').value.trim(),a=current();
  if(a<=0){$('err').innerHTML=msg('Saisissez un montant.');return}
  if(m!=='Espèces'&&!r){$('err').innerHTML=msg('Indiquez la référence de la transaction.');return}
  S.pays.push({mode:m,montant:a,ref:r});renderPay();
}
var searchClient=debounce(function(){
  api('clients',{q:$('cq').value}).then(function(j){
    if(!$('cres'))return;window._cl={};
    $('cres').innerHTML=(j.items||[]).map(function(c){window._cl[c.id]=c;return '<div class="item" onclick="setClient('+c.id+')"><div class="t">'+esc(c.nom)+'</div><div class="m">solde '+fmt(c.solde)+'</div></div>'}).join('');
  });
},250);
function setClient(id){S.client=id?window._cl[id]:null;renderPay()}
function validate(){
  var t=totals().total,pays=S.pays.slice(),a=current(),m=$('pm').value,r=$('pr')?$('pr').value.trim():'';
  if(a>0){if(m!=='Espèces'&&!r){$('err').innerHTML=msg('Indiquez la référence de la transaction.');return}pays.push({mode:m,montant:a,ref:r})}
  var btn=document.querySelector('button.ok');btn.disabled=true;btn.textContent='Enregistrement…';
  api('sell',{lines:S.cart.map(function(l){return {id:l.id,qty:l.qty,rem:l.rem}}),payments:pays,client_id:S.client?S.client.id:null,remise_globale:S.rg}).then(function(j){
    if(!j.ok){btn.disabled=false;btn.textContent='✓ ENCAISSER';$('err').innerHTML=msg(j.error);return}
    S.cart=[];S.pays=[];S.client=null;S.rg=0;
    $('v').innerHTML='<div class="card center"><div style="font-size:54px">✅</div><h2>Vente enregistrée</h2><div class="muted">'+esc(j.facture_no)+'</div><div style="font-size:26px;font-weight:800;margin:8px 0">'+fmt(j.total)+'</div>'+
      (j.rendu>0?'<div class="msg k">Monnaie à rendre : '+fmt(j.rendu)+'</div>':'')+(j.reste>0?'<div class="msg e">Reste à crédit : '+fmt(j.reste)+'</div>':'')+
      '<div class="row" style="margin-top:12px"><button onclick="openDoc(\'facture\','+j.vente_id+',\'ticket\')">Ticket</button><button onclick="openDoc(\'facture\','+j.vente_id+',\'a4\')">Facture A4</button></div>'+
      '<button class="p" style="width:100%;margin-top:10px" onclick="go(\'sell\')">Nouvelle vente</button></div>';
  });
}
function openDoc(kind,id,fmt_){window.open('/m/doc?kind='+kind+'&id='+id+'&fmt='+fmt_+'&t='+encodeURIComponent(S.token),'_blank')}
/* ---------- dettes ---------- */
function debts(){
  $('v').innerHTML='<div class="card"><h2>Dettes à encaisser</h2><input id="dq" placeholder="Client ou n° de facture…" oninput="loadDebts()"><div id="dl"></div></div>';loadDebts();
}
var loadDebts=debounce(function(){
  api('debts',{q:$('dq')?$('dq').value:''}).then(function(j){
    if(!$('dl'))return;window._d={};
    $('dl').innerHTML=!j.ok?msg(j.error):(j.items.map(function(d){window._d[d.id]=d;return '<div class="item" onclick="payForm('+d.id+')"><div><div class="t">'+esc(d.client)+'</div><div class="m">'+esc(d.facture_no)+' · '+esc(d.date)+'</div></div><div><b style="color:var(--bad)">'+fmt(d.reste)+'</b></div></div>'}).join('')||'<div class="muted">Aucune dette en cours.</div>');
  });
},250);
function sheet(html){var s=document.createElement('div');s.className='sheet';s.id='sheet';s.innerHTML='<div>'+html+'</div>';s.addEventListener('click',function(e){if(e.target===s)s.remove()});document.body.appendChild(s)}
function closeSheet(){var s=$('sheet');if(s)s.remove()}
function payForm(id){
  var d=window._d[id];
  sheet('<h2>'+esc(d.client)+'</h2><div class="muted">'+esc(d.facture_no)+' · reste dû <b>'+fmt(d.reste)+'</b></div><label>Montant reçu</label><input id="dm" inputmode="decimal" value="'+d.reste+'">'+
   '<label>Mode</label><select id="dmo" onchange="$(\'drb\').classList.toggle(\'hide\',this.value===\'Espèces\')">'+S.modes.map(function(m){return '<option>'+esc(m)+'</option>'}).join('')+'</select>'+
   '<div id="drb" class="hide"><label>Référence</label><input id="dr"></div><div id="de"></div><button class="ok" style="margin-top:10px" onclick="payDebt('+id+')">✓ ENCAISSER</button>');
}
function payDebt(id){
  var m=num($('dm').value),mo=$('dmo').value,r=$('dr').value.trim();
  if(mo!=='Espèces'&&!r){$('de').innerHTML=msg('Indiquez la référence.');return}
  api('pay_debt',{vente_id:id,montant:m,mode:mo,ref:r}).then(function(j){
    if(!j.ok){$('de').innerHTML=msg(j.error);return}
    $('sheet').firstChild.innerHTML='<div class="center"><div style="font-size:54px">✅</div><h2>Règlement enregistré</h2><button style="width:100%;margin:8px 0" onclick="openDoc(\'recu\','+j.reglement_id+',\'ticket\')">Voir le reçu (ticket)</button>'+
      '<button style="width:100%;margin-bottom:8px" onclick="openDoc(\'recu\','+j.reglement_id+',\'a4\')">Reçu A4</button><button class="p" style="width:100%" onclick="closeSheet();go(\'debts\')">Fermer</button></div>';
  });
}
/* ---------- stock ---------- */
function stock(){
  $('v').innerHTML='<div class="card"><h2>Stock</h2><input id="sq" placeholder="Rechercher un article…" oninput="loadStock()"><label><input type="checkbox" id="sl" style="width:auto" onchange="loadStock()"> Seulement les articles à réapprovisionner</label><div id="sl2"></div></div>';loadStock();
}
var loadStock=debounce(function(){
  api('stock',{q:$('sq')?$('sq').value:'',only_low:$('sl')&&$('sl').checked}).then(function(j){
    if(!$('sl2'))return;
    $('sl2').innerHTML=!j.ok?msg(j.error):(j.items.map(function(a){return '<div class="item"><div><div class="t">'+esc(a.designation)+'</div><div class="m">'+esc(a.categorie)+' · '+fmt(a.prix_vente)+'</div></div>'+stockBadge(a)+'</div>'}).join('')||'<div class="muted">Aucun article.</div>');
  });
},250);
/* ---------- installation comme application (PWA) ---------- */
function standalone(){return (window.matchMedia&&window.matchMedia('(display-mode: standalone)').matches)||window.navigator.standalone===true}
function installCard(){
  if(standalone())return '<div class="card"><h2>Application installée</h2><div class="muted">✓ Vous utilisez l\u2019application installée sur ce téléphone.</div></div>';
  var h='<div class="card"><h2>Installer l\u2019application</h2>';
  if(S.inst){h+='<div class="muted">Ajoutez la boutique sur l\u2019écran d\u2019accueil, avec son logo, comme une vraie application.</div><button class="p" style="width:100%;margin-top:8px" onclick="installApp()">Installer l\u2019application</button>'}
  else if(/iphone|ipad|ipod/i.test(navigator.userAgent)){h+='<div>Sur iPhone / iPad, avec <b>Safari</b> : touchez le bouton <b>Partager</b> (carré avec une flèche), puis <b>« Sur l\u2019écran d\u2019accueil »</b>.</div>'}
  else if(location.protocol!=='https:'){h+='<div>L\u2019installation complète demande l\u2019adresse sécurisée <b>https</b> : ouvrez la page par l\u2019accès <b>Internet</b> (QR code de l\u2019onglet Téléphone sur l\u2019ordinateur). Sinon : menu ⋮ du navigateur > <b>Ajouter à l\u2019écran d\u2019accueil</b>.</div>'}
  else{h+='<div>Touchez le menu <b>⋮</b> du navigateur puis <b>« Installer l\u2019application »</b> (ou « Ajouter à l\u2019écran d\u2019accueil »).</div>'}
  return h+'</div>';
}
function installApp(){if(!S.inst)return;S.inst.prompt();S.inst.userChoice.then(function(){S.inst=null;go(S.tab)})}
window.addEventListener('beforeinstallprompt',function(e){e.preventDefault();S.inst=e;if(S.user&&(S.tab==='home'||S.tab==='more'))go(S.tab)});
window.addEventListener('appinstalled',function(){S.inst=null;if(S.user)go(S.tab)});
if('serviceWorker' in navigator){window.addEventListener('load',function(){navigator.serviceWorker.register('/m/sw.js',{scope:'/m/'}).catch(function(){})})}
/* ---------- plus : dépense, mot de passe ---------- */
function more(note){
  $('v').innerHTML=(note?msg(note):'')+installCard()+'<div class="card"><h2>Enregistrer une dépense</h2><label>Motif</label><input id="el" placeholder="Transport, électricité…"><label>Montant (FCFA)</label><input id="em" inputmode="decimal"><div id="ee"></div><button class="p" style="width:100%;margin-top:8px" onclick="addExpense()">Enregistrer la dépense</button></div>'+
   '<div class="card"><h2>Mon mot de passe</h2><label>Mot de passe actuel</label><input id="po" type="password"><label>Nouveau mot de passe (8 caractères min., lettres + chiffres)</label><input id="pn" type="password"><div id="pe"></div><button style="width:100%;margin-top:8px" onclick="chgPw()">Changer le mot de passe</button></div>'+
   '<button class="bad" style="width:100%" onclick="logout()">Se déconnecter</button>';
}
function addExpense(){
  api('expense',{libelle:$('el').value,montant:num($('em').value)}).then(function(j){
    if(!j.ok){$('ee').innerHTML=msg(j.error);return}
    $('ee').innerHTML=msg('Dépense enregistrée.',1)+'<button style="width:100%" onclick="openDoc(\'depense\','+j.depense_id+',\'ticket\')">Voir le bon de dépense</button>';$('el').value='';$('em').value='';
  });
}
function chgPw(){api('change_password',{old:$('po').value,new:$('pn').value}).then(function(j){$('pe').innerHTML=msg(j.ok?j.msg:j.error,j.ok)})}
(function(){var h=/[#&]c=([A-Za-z0-9]+)/.exec(location.hash);if(h){localStorage.setItem('gb_code',h[1].toUpperCase());try{history.replaceState(null,'',location.pathname)}catch(e){}}})();
if(S.token){api('me').then(function(j){if(j.ok){S.user=j.user;S.tva=j.tva_taux||0;S.modes=j.modes;setNom(j.boutique);ready(false)}else showLogin()})}else showLogin();
</script></body></html>
"""
