# -*- coding: utf-8 -*-
"""
GESTION DE BOUTIQUE - Mode réseau (reseau_boutique.py)

Plusieurs ordinateurs de la boutique (caisses, magasin, bureau) peuvent travailler en même temps sur LES MÊMES données :
  - Poste unique : fonctionnement habituel (aucun réseau).
  - Serveur      : l'ordinateur qui garde la base de données. Il s'utilise normalement ET répond aux autres postes.
  - Client       : un autre ordinateur du même réseau (même box / même Wi-Fi). Il n'a pas de base de données :
                   toutes ses opérations passent par le serveur.

Sécurité : chaque demande est signée (HMAC-SHA256) avec le CODE D'ACCÈS réseau, qui ne circule jamais ; les demandes
rejouées sont refusées ; les droits (admin / gérant / vendeur / magasinier) sont revérifiés SUR LE SERVEUR à chaque opération ;
5 codes faux = blocage de l'adresse pendant 60 secondes. Les échanges ne sont pas chiffrés : à utiliser sur le réseau
local de la boutique (pas sur Internet).
"""
import base64
import collections
import gzip
import hashlib
import hmac
import http.client
import inspect
import json
import os
import secrets
import socket
import sqlite3
import threading
import time
import traceback
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import database_boutique as D

PROTO = 1
DEFAULT_PORT = 8765
NET_PATH = os.path.join(D.BASE_DIR, 'reseau_boutique.json')
LOCK = threading.RLock()            # une seule opération à la fois sur la base (serveur + poste du serveur)
SESSION_HEURES = 12
SKEW_MAX = 300                      # écart d'horloge toléré entre deux PC (secondes)
MAX_BODY = 60 * 1024 * 1024
LAST_ATTRS = ('last_article_id', 'last_client_id', 'last_depense_id', 'last_mouvement_id')     # identifiants renvoyés au poste client
BLOCKED = {'backup', 'close', 'restaurer', 'copie_externe', 'liste_sauvegardes', 'etat_sauvegarde', 'verifier_sauvegarde'}     # opérations réservées au poste serveur
_ADMIN, _GER, _STOCK, _VENTE = {'admin'}, {'admin', 'gerant'}, {'admin', 'gerant', 'magasinier'}, {'admin', 'gerant', 'vendeur'}
POLICY = {                          # droits minimaux exigés par le serveur (l'interface seule ne suffit pas)
    'add_user': _ADMIN, 'reset_password': _ADMIN, 'update_user': _ADMIN, 'delete_user': _ADMIN, 'get_users': _GER, 'get_audit': _GER, 'verify_audit': _GER,
    'save_categorie': _GER, 'archive_article': _GER, 'annuler_vente': _GER, 'retourner': _GER, 'rouvrir_cloture': _GER,
    'payer_fournisseur': _GER, 'delete_pack': _GER, 'resume_periode': _GER, 'inventaire_periode': _GER, 'journaliser_inventaire': _GER, 'journaliser_audit': _GER, 'ventes_par_famille': _GER, 'top_articles': _GER,
    'ventes_par_jour': _GER, 'ventes_par_vendeur': _GER, 'top_clients': _GER, 'articles_dormants': _STOCK,
    'save_article': _STOCK, 'ajuster_stock': _STOCK, 'inventaire': _STOCK, 'acheter': _STOCK, 'import_articles': _STOCK,
    'save_pack': _STOCK, 'save_fournisseur': _STOCK, 'vendre': _VENTE, 'encaisser_credit': _VENTE, 'add_depense': _VENTE,
}
CACHEABLE = {'search_articles', 'get_categories', 'get_clients', 'get_fournisseurs', 'get_packs', 'valeur_stock',
             'alertes_stock', 'suggestion_reapprovisionnement', 'get_article', 'get_users'}
# L'identité du vendeur / caissier est celle de la SESSION : un poste ne peut pas se faire passer pour un autre.
FORCE_USER = {'vendre': (4, 'vendeur'), 'encaisser_credit': (4, 'caissier')}


# ---------------------------------------------------------------- réglages locaux (propres à chaque PC)
def load_net():
    cfg = {'mode': 'autonome', 'host': '', 'port': DEFAULT_PORT, 'code': ''}
    try:
        with open(NET_PATH, encoding='utf-8') as f:
            cfg.update({k: v for k, v in json.load(f).items() if k in cfg})
    except (OSError, ValueError):
        pass
    if cfg['mode'] not in ('autonome', 'serveur', 'client'):
        cfg['mode'] = 'autonome'
    try:
        cfg['port'] = int(cfg['port'])
    except (TypeError, ValueError):
        cfg['port'] = DEFAULT_PORT
    return cfg


def save_net(cfg):
    with open(NET_PATH, 'w', encoding='utf-8') as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


def new_code():
    return secrets.token_hex(4).upper()


def lan_ips():
    """Adresses de cet ordinateur sur le réseau local (à saisir sur les postes clients)."""
    ips = set()
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(('10.255.255.255', 1))      # aucun paquet envoyé : sert seulement à connaître la bonne carte réseau
        ips.add(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    try:
        ips.update(socket.gethostbyname_ex(socket.gethostname())[2])
    except OSError:
        pass
    return sorted(i for i in ips if not i.startswith('127.'))


# ---------------------------------------------------------------- sérialisation JSON (lignes SQL, octets, tuples...)
class RRow:
    """Ligne reçue du serveur : se lit comme sqlite3.Row (row['nom'], row[0], dict(row), row.keys())."""
    __slots__ = ('_k', '_v', '_i')

    def __init__(self, keys, values):
        self._k, self._v = list(keys), list(values)
        self._i = {k: i for i, k in enumerate(self._k)}

    def __getitem__(self, key):
        return self._v[key] if isinstance(key, (int, slice)) else self._v[self._i[key]]

    def keys(self):
        return list(self._k)

    def get(self, key, default=None):
        return self._v[self._i[key]] if key in self._i else default

    def __iter__(self):
        return iter(self._v)

    def __len__(self):
        return len(self._v)

    def __repr__(self):
        return f"RRow({dict(zip(self._k, self._v))})"


def enc(o):
    if o is None or isinstance(o, (bool, int, float, str)):
        return o
    if isinstance(o, (bytes, bytearray, memoryview)):
        return {'__b': base64.b64encode(bytes(o)).decode()}
    if isinstance(o, (sqlite3.Row, RRow)):
        return {'__r': [list(o.keys()), [enc(v) for v in tuple(o)]]}
    if isinstance(o, tuple):
        return {'__t': [enc(x) for x in o]}
    if isinstance(o, (set, frozenset)):
        return {'__s': [enc(x) for x in o]}
    if isinstance(o, list):
        return [enc(x) for x in o]
    if isinstance(o, dict):
        if all(isinstance(k, str) and not k.startswith('__') for k in o):
            return {k: enc(v) for k, v in o.items()}
        return {'__d': [[enc(k), enc(v)] for k, v in o.items()]}
    if isinstance(o, (date,)):
        return o.isoformat()
    raise TypeError(f"Type non transmissible : {type(o).__name__}")


def dec(o):
    if isinstance(o, list):
        return [dec(x) for x in o]
    if isinstance(o, dict):
        if len(o) == 1:
            if '__b' in o:
                return base64.b64decode(o['__b'])
            if '__t' in o:
                return tuple(dec(x) for x in o['__t'])
            if '__s' in o:
                return {dec(x) for x in o['__s']}
            if '__d' in o:
                return {dec(k): dec(v) for k, v in o['__d']}
            if '__r' in o:
                keys, vals = o['__r']
                return RRow(keys, [dec(v) for v in vals])
        return {k: dec(v) for k, v in o.items()}
    return o


def _sig(code, ts, nonce, body):
    return hmac.new(code.encode(), f"{ts}.{nonce}.".encode() + body, hashlib.sha256).hexdigest()


def _file_b64(path):
    try:
        with open(path, 'rb') as f:
            return base64.b64encode(f.read()).decode()
    except OSError:
        return None


# ---------------------------------------------------------------- SERVEUR
class _Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, *a):
        pass

    def _send(self, status, payload=None, raw=None):
        data = raw if raw is not None else json.dumps(payload).encode()
        gz = len(data) > 2048 and 'gzip' in self.headers.get('Accept-Encoding', '')
        if gz:
            data = gzip.compress(data, 3)
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(data)))
        if gz:
            self.send_header('Content-Encoding', 'gzip')
        self.end_headers()
        self.wfile.write(data)

    def _origine(self):
        """(adresse réelle du visiteur, vient_du_tunnel). Le tunnel (cloudflared) se connecte depuis cet ordinateur : on ne fait confiance
        à ses en-têtes que pour une connexion locale (127.0.0.1), jamais pour une connexion venant du réseau."""
        local = self.client_address[0] in ('127.0.0.1', '::1')
        reel = self.headers.get('CF-Connecting-IP', '') if local else ''
        via = bool(local and (reel or self.headers.get('Cf-Ray')))
        return (reel or self.client_address[0]), via

    def do_GET(self):
        """Pages du téléphone : /m (application), /m/doc (factures PDF). L'adresse sans /m est redirigée vers /m."""
        host = self.server.host
        ip, via = self._origine()
        host.note_hit(ip, self.path, self.headers.get('User-Agent', ''), via)
        if self.path in ('/', ''):
            self.send_response(302)
            self.send_header('Location', '/m')
            self.send_header('Content-Length', '0')
            self.end_headers()
            return
        if not self.path.startswith('/m'):
            self._send(404, raw=b'{"ok": false}')
            return
        import mobile_boutique as MB
        status, ctype, data = MB.handle_get(host, self.path)
        self.send_response(status)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(data)

    def _mobile_api(self, host, ip, body):
        import mobile_boutique as MB
        try:
            resp = MB.handle_api(host, json.loads(body or b'{}'), ip)
        except Exception as e:  # noqa
            traceback.print_exc()
            resp = {'ok': False, 'error': f"Erreur du serveur : {type(e).__name__}"}
        self._send(200, resp)

    def do_POST(self):
        host = self.server.host
        ip, via = self._origine()
        if via and self.path != '/m/api':                          # depuis Internet : seule l'API du téléphone est exposée, jamais le serveur des postes
            self._send(403, {'ok': False, 'error': {'type': 'AuthError', 'msg': "Non disponible par Internet."}})
            return
        n = int(self.headers.get('Content-Length') or 0)
        if n > MAX_BODY:
            self._send(413, {'ok': False, 'error': {'type': 'Error', 'msg': 'Demande trop volumineuse.'}})
            return
        body = self.rfile.read(n)
        if self.path == '/m/api':                                  # téléphone : code d'accès + identifiants vérifiés à la connexion
            self._mobile_api(host, ip, body)
            return
        if host.blocked(ip):
            self._send(429, {'ok': False, 'error': {'type': 'AuthError', 'msg': "Trop de codes faux : patientez une minute."}})
            return
        ts, nonce, sig = (self.headers.get(h, '') for h in ('X-Ts', 'X-Nonce', 'X-Sig'))
        try:
            fresh = abs(time.time() - float(ts)) < SKEW_MAX
        except ValueError:
            fresh = False
        if not (nonce and fresh and hmac.compare_digest(sig, _sig(host.code, ts, nonce, body))):
            host.fail(ip)
            self._send(403, {'ok': False, 'error': {'type': 'AuthError', 'msg': "Code d'accès réseau incorrect (ou les horloges des "
                                                                              "deux ordinateurs sont décalées de plus de 5 minutes)."}})
            return
        host.ok(ip)
        raw = host.replay(nonce)
        if raw is None:
            try:
                resp = host.handle(json.loads(body), ip)
            except Exception as e:  # noqa
                traceback.print_exc()
                resp = {'ok': False, 'error': {'type': 'Error', 'msg': f"{type(e).__name__}: {e}"}}
            raw = json.dumps(resp).encode()
            host.remember(nonce, raw)
        self._send(200, raw=raw)


class HostServer:
    """Serveur réseau : expose la base de données courante (get_db) aux postes clients."""

    def __init__(self, get_db, code, port=DEFAULT_PORT, lock=LOCK):
        self.get_db, self.code, self.port, self.lock = get_db, code, port, lock
        self.sessions, self.httpd, self.thread = {}, None, None
        self.hits = []                                   # derniers accès web (téléphones) : {'t', 'ip', 'path', 'ua'}
        self._fails, self._block, self._seen = {}, {}, collections.OrderedDict()
        self._mutex = threading.Lock()

    # --- cycle de vie
    def start(self):
        self.httpd = ThreadingHTTPServer(('0.0.0.0', self.port), _Handler)
        self.httpd.daemon_threads = True
        self.httpd.host = self
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def stop(self):
        if self.httpd:
            self.httpd.shutdown()
            self.httpd.server_close()
            self.httpd = None

    def clients(self, minutes=None):
        """Postes connectés : [(identifiant, nom, rôle, adresse IP, dernière activité)]."""
        now = time.time()
        out = [(s['user']['username'], s['actor'], s['role'], s['ip'], s['last']) for s in list(self.sessions.values())
               if minutes is None or now - s['last'] < minutes * 60]
        return sorted(out, key=lambda x: -x[4])

    # --- protection contre les codes faux et les rejeux
    def blocked(self, ip):
        return time.time() < self._block.get(ip, 0)

    def fail(self, ip):
        with self._mutex:
            self._fails[ip] = self._fails.get(ip, 0) + 1
            if self._fails[ip] >= 5:
                self._fails[ip] = 0
                self._block[ip] = time.time() + 60

    def ok(self, ip):
        self._fails.pop(ip, None)

    def replay(self, nonce):
        """Demande déjà traitée (le client a réessayé après une coupure) : on renvoie la même réponse, sans refaire l'action."""
        with self._mutex:
            hit = self._seen.get(nonce)
        return hit[1] if hit else None

    def remember(self, nonce, raw):
        with self._mutex:
            self._seen[nonce] = (time.time(), raw if len(raw) < 65536 else None)
            while len(self._seen) > 2000 or (self._seen and time.time() - next(iter(self._seen.values()))[0] > 600):
                self._seen.popitem(last=False)
            if self._seen.get(nonce, (0, 1))[1] is None:      # grosse réponse (lecture) : on la refera au besoin
                self._seen.pop(nonce, None)

    # --- traitement des demandes
    @staticmethod
    def _err(typ, msg):
        return {'ok': False, 'error': {'type': typ, 'msg': msg}}

    def _version(self, db):
        return [db.conn.total_changes, db.conn.execute("PRAGMA data_version").fetchone()[0], date.today().isoformat(), db.path]

    def note_hit(self, ip, path, ua='', via=False):
        """Mémorise les derniers accès web (pour l'assistant téléphone : « un appareil a-t-il atteint le serveur ? »)."""
        self.hits.append({'t': time.time(), 'ip': ip, 'path': path[:60], 'ua': ua[:80], 'internet': bool(via)})
        del self.hits[:-30]

    def invoke(self, sess, method, args=(), kwargs=None, attrs=False):
        """Appelle une méthode de la base au nom d'une session (utilisé par l'interface téléphone) : mêmes droits, même verrou,
        même identité imposée que pour un poste réseau."""
        args, kwargs = list(args), dict(kwargs or {})
        if method.startswith('_') or method in BLOCKED or not callable(getattr(D.Database, method, None)):
            raise PermissionError(f"Opération non disponible à distance : {method}")
        need = POLICY.get(method)
        if need and sess['role'] not in need:
            raise PermissionError("Action non autorisée pour votre rôle.")
        if method in FORCE_USER:
            pos, key = FORCE_USER[method]
            args, kwargs[key] = args[:pos], sess['actor']
        with self.lock:
            db = self.get_db()
            saved = (db.actor, db.actor_role)
            db.actor, db.actor_role = sess['actor'], sess['role']
            try:
                res = getattr(db, method)(*args, **kwargs)
                return (res, {k: getattr(db, k, None) for k in LAST_ATTRS}) if attrs else res
            finally:
                db.actor, db.actor_role = saved

    def handle(self, req, ip):
        op = req.get('op')
        if op == 'hello':
            db = self.get_db()
            return {'ok': True, 'proto': PROTO, 'annee': os.path.basename(db.path)[9:-3], 'ecole': D.get_config().get('boutique', '')}
        if op == 'config':
            cfg = {k: v for k, v in D.get_config().items() if k not in ('tunnel_token', 'backup_externe', 'logo')}      # jamais de secret vers les postes
            out = {'ok': True, 'config': cfg}
            try:                                                   # le logo voyage avec la configuration : les postes clients l'impriment aussi
                lg = (D.get_config().get('logo') or '').strip()
                if lg and os.path.isfile(lg) and os.path.getsize(lg) < 600_000:
                    with open(lg, 'rb') as f:
                        out['logo_png'] = base64.b64encode(f.read()).decode()
            except OSError:
                pass
            return out
        if op == 'login':
            with self.lock:
                db = self.get_db()
                saved = (db.actor, db.actor_role)
                try:
                    user = db.authenticate(str(req.get('username', '')).strip(), str(req.get('password', '')))
                    msg = db.auth_msg
                    if user:
                        db.log("Poste réseau connecté", ip, user=user['username'])
                finally:
                    db.actor, db.actor_role = saved
            out = {'ok': True, 'user': user, 'auth_msg': msg}
            if user:
                tok = secrets.token_urlsafe(24)
                self.sessions[tok] = {'user': user, 'actor': user['username'], 'role': user['role'], 'ip': ip,
                                      'last': time.time(), 'since': time.time()}
                out['token'] = tok
            return out
        sess = self.sessions.get(req.get('token'))
        if not sess or time.time() - sess['last'] > SESSION_HEURES * 3600:
            self.sessions.pop(req.get('token'), None)
            return self._err('SessionError', "Session expirée : reconnectez-vous.")
        sess['last'] = time.time()
        if op == 'logout':
            self.sessions.pop(req.get('token'), None)
            return {'ok': True}
        if op == 'ver':
            with self.lock:
                return {'ok': True, 'ver': self._version(self.get_db())}
        if op == 'years':
            return {'ok': True, 'years': D.list_years()}
        if op != 'call':
            return self._err('Error', f"Opération inconnue : {op}")
        method = str(req.get('method', ''))
        fn_cls = getattr(D.Database, method, None)
        if method.startswith('_') or method in BLOCKED or not callable(fn_cls):
            return self._err('PermissionError', f"Opération non disponible à distance : {method}")
        need = POLICY.get(method)
        if need and sess['role'] not in need:
            return self._err('PermissionError', "Action non autorisée pour votre rôle.")
        args, kwargs = dec(req.get('args', [])), dec(req.get('kwargs', {}))
        if method in FORCE_USER:
            pos, key = FORCE_USER[method]
            args, kwargs[key] = list(args)[:pos], sess['actor']
        with self.lock:
            db = self.get_db()
            saved = (db.actor, db.actor_role)
            db.actor, db.actor_role = sess['actor'], sess['role']
            try:
                result = getattr(db, method)(*args, **kwargs)
                return {'ok': True, 'result': enc(result), 'attrs': {k: getattr(db, k, None) for k in LAST_ATTRS},
                        'ver': self._version(db)}
            except PermissionError as e:
                return self._err('PermissionError', str(e))
            except sqlite3.IntegrityError as e:
                return self._err('IntegrityError', str(e))
            except ValueError as e:
                return self._err('ValueError', str(e))
            finally:
                db.actor, db.actor_role = saved


class LockedDB:
    """Base de données du poste SERVEUR : chaque appel prend le même verrou que les requêtes des postes clients."""

    def __init__(self, real, lock=LOCK):
        object.__setattr__(self, '_real', real)
        object.__setattr__(self, '_lock', lock)

    def __getattr__(self, name):
        v = getattr(self._real, name)
        if inspect.isroutine(v) and not name.startswith('_'):     # seulement les méthodes (pas conn : une connexion est « appelable »)
            def locked(*a, **k):
                with self._lock:
                    return v(*a, **k)
            return locked
        return v

    def __setattr__(self, name, value):
        setattr(self._real, name, value)


# ---------------------------------------------------------------- CLIENT
class NetworkError(OSError):
    """Serveur injoignable ou connexion coupée."""


class AuthError(Exception):
    """Code d'accès réseau refusé."""


class SessionError(Exception):
    """Session expirée : il faut se reconnecter."""


class RemoteDatabase:
    """Se comporte comme Database, mais exécute tout sur le serveur."""

    def __init__(self, host, port, code, timeout=30):
        self.host, self.port, self.code, self.timeout = host, int(port), code, timeout
        self.token, self._conn, self._cache = None, None, {}
        self.actor, self.actor_role, self.auth_msg = '-', '', ''
        self.last_article_id = self.last_client_id = self.last_depense_id = self.last_mouvement_id = None

    # --- transport
    def _close(self):
        try:
            if self._conn:
                self._conn.close()
        except OSError:
            pass
        self._conn = None

    def _post(self, payload):
        body = json.dumps(payload).encode()
        ts, nonce = f"{time.time():.3f}", secrets.token_hex(8)
        headers = {'Content-Type': 'application/json', 'X-Ts': ts, 'X-Nonce': nonce, 'Accept-Encoding': 'gzip',
                   'X-Sig': _sig(self.code, ts, nonce, body)}
        for attempt in (1, 2):      # un 2e essai est sans danger : le serveur reconnaît le même « nonce » et ne refait pas l'action
            try:
                if self._conn is None:
                    self._conn = http.client.HTTPConnection(self.host, self.port, timeout=self.timeout)
                self._conn.request('POST', '/rpc', body, headers)
                r = self._conn.getresponse()
                data = r.read()
                if r.getheader('Content-Encoding') == 'gzip':
                    data = gzip.decompress(data)
                return json.loads(data)
            except (OSError, http.client.HTTPException, ValueError) as e:
                self._close()
                if attempt == 2:
                    raise NetworkError(f"Serveur injoignable ({self.host}:{self.port}). Vérifiez que le poste serveur est allumé, "
                                       f"que l'application y est ouverte et que le câble / Wi-Fi fonctionne.\n({type(e).__name__})") from e

    def _rpc(self, op, **kw):
        resp = self._post({'op': op, 'token': self.token, **kw})
        if not resp.get('ok'):
            e = resp.get('error', {})
            typ, msg = e.get('type', 'Error'), e.get('msg', 'Erreur inconnue.')
            if typ == 'AuthError':
                raise AuthError(msg)
            if typ == 'SessionError':
                self.token = None
                raise SessionError(msg)
            if typ == 'PermissionError':
                raise PermissionError(msg)
            if typ == 'IntegrityError':
                raise sqlite3.IntegrityError(msg)
            if typ == 'ValueError':
                raise ValueError(msg)
            raise RuntimeError(msg)
        if 'attrs' in resp:
            for k in LAST_ATTRS:
                setattr(self, k, resp['attrs'].get(k))
        return resp

    # --- opérations propres au mode réseau
    def hello(self):
        return self._rpc('hello')

    def sync_config(self):
        """Récupère le nom, l'adresse, la TVA, etc. de la boutique depuis le serveur (gardés en mémoire seulement)."""
        cfg = D.get_config()
        rep_cfg = self._rpc('config')
        cfg.update({k: v for k, v in rep_cfg['config'].items() if k not in ('backup_externe', 'tunnel_token', 'logo')})
        if rep_cfg.get('logo_png'):                                # copie locale du logo du serveur
            try:
                chemin = os.path.join(D.BASE_DIR, 'logo.png')
                os.makedirs(D.BASE_DIR, exist_ok=True)
                with open(chemin, 'wb') as f:
                    f.write(base64.b64decode(rep_cfg['logo_png']))
                cfg['logo'] = chemin
            except (OSError, ValueError):
                pass
        return cfg

    def years(self):
        return self._rpc('years')['years']

    def authenticate(self, username, password):
        r = self._rpc('login', username=username, password=password)
        self.auth_msg = r.get('auth_msg', '')
        user = r.get('user')
        if user:
            self.token = r['token']
        return user

    def logout(self):
        if self.token:
            try:
                self._rpc('logout')
            except (NetworkError, SessionError):
                pass
        self.token = None
        self._close()

    def _ver(self):
        return self._rpc('ver')['ver']

    # --- toutes les autres méthodes de Database
    def __getattr__(self, name):
        if name.startswith('__'):
            raise AttributeError(name)

        def call(*a, **k):
            return self._call(name, a, k)
        return call

    def _call(self, name, a, k):
        if name == 'backup':      # les sauvegardes se font sur le poste serveur
            return None
        if name == 'close':
            self.logout()
            return None
        cacheable = name in CACHEABLE
        if cacheable:       # on ne retransmet pas les gros résultats tant que la base n'a pas changé
            ver = self._ver()
            key = (name, repr(a), repr(sorted(k.items())))
            hit = self._cache.get(key)
            if hit and hit[0] == ver:
                return list(hit[1]) if isinstance(hit[1], list) else hit[1]
        res = dec(self._rpc('call', method=name, args=enc(list(a)), kwargs=enc(k))['result'])
        if cacheable:
            if len(self._cache) > 200:
                self._cache.clear()
            self._cache[key] = (ver, res)
        return res
