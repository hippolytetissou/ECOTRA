# -*- coding: utf-8 -*-
"""
GESTION DE BOUTIQUE (librairie, papeterie, fournitures, équipement de bureau)
Base de données - database_boutique.py
Une base par exercice (année) dans le dossier « annees ».
Inspirée de l'architecture de l'application CBJB : SQLite, rôles, journal d'audit chaîné,
clôture de caisse, sauvegardes.
"""
import math
import os
import re
import json
import shutil
import sqlite3
import hashlib
import secrets
import unicodedata
from datetime import datetime, date, timedelta

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ANNEES_DIR = os.path.join(BASE_DIR, 'annees')
BACKUP_DIR = os.path.join(BASE_DIR, 'sauvegardes')
CONFIG_PATH = os.path.join(BASE_DIR, 'config_boutique.json')
MAX_ECHECS, BLOCAGE_MIN = 5, 5
WEAK = {'123456', '12345678', 'password', 'motdepasse', 'admin', 'admin123', 'azerty', 'qwerty', '000000', '1234'}

MODES = ["Espèces", "Orange Money", "MTN MoMo", "Virement", "Chèque", "Crédit client"]
REF_LABELS = {"Orange Money": "N° de transaction", "MTN MoMo": "N° de transaction",
              "Virement": "N° de virement", "Chèque": "N° de chèque"}
# Rôles : admin (tout), gerant (stock, prix, achats, rapports), vendeur (vente, clients), magasinier (stock, achats)
ROLES = {'admin': 'Administrateur', 'gerant': 'Gérant', 'vendeur': 'Vendeur / Caissier', 'magasinier': 'Magasinier'}

DEFAULT_CONFIG = {'boutique': "MA GRANDE BOUTIQUE", 'prefixe': "MGB", 'adresse': "", 'telephone': "",
                  'tva_taux': 0.0, 'seuil_remise_vendeur': 10.0, 'message_recu': "Merci de votre confiance !",
                  'backup_externe': "", 'logo': "", 'format_recu': "ticket", 'niu': "", 'rccm': "", 'mentions': "",
                  'sauvegarde_auto_min': 20.0, 'ouvrir_recus': "oui", 'tunnel_token': "", 'tunnel_adresse': ""}
_CFG = {}

# Catégories de départ (modifiables dans l'application)
CATEGORIES = [
    ("Livres scolaires - Primaire", "livre"), ("Livres scolaires - Secondaire", "livre"),
    ("Livres - Romans & littérature", "livre"), ("Livres - Dictionnaires & référence", "livre"),
    ("Livres - Religion & développement personnel", "livre"), ("Livres - Jeunesse & BD", "livre"),
    ("Cahiers & papeterie", "fourniture"), ("Écriture (stylos, crayons...)", "fourniture"),
    ("Matériel de géométrie & dessin", "fourniture"), ("Sacs & cartables", "fourniture"),
    ("Mobilier de bureau", "bureau"), ("Informatique & électronique", "bureau"),
    ("Consommables de bureau", "bureau"), ("Outils & divers", "autre"),
]
UNITES = ["pièce", "lot", "ramette", "paquet", "boîte", "carton", "mètre"]


# ---------------------------------------------------------------- utilitaires
def get_config():
    if not _CFG:
        _CFG.update(DEFAULT_CONFIG)
        try:
            with open(CONFIG_PATH, encoding='utf-8') as f:
                _CFG.update({k: v for k, v in json.load(f).items() if k in DEFAULT_CONFIG})
        except (OSError, ValueError):
            pass
    return _CFG


def set_config(**kw):
    cfg = get_config()
    cfg.update({k: v for k, v in kw.items() if k in DEFAULT_CONFIG})
    with open(CONFIG_PATH, 'w', encoding='utf-8') as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


def norm(txt):
    t = unicodedata.normalize('NFD', str(txt or '')).lower()
    return ''.join(c for c in t if unicodedata.category(c) != 'Mn')


def fcfa(v):
    return f"{round(v or 0):,}".replace(',', ' ') + " FCFA"


def to_fr(iso):
    try:
        return datetime.strptime(str(iso)[:10], '%Y-%m-%d').strftime('%d/%m/%Y')
    except ValueError:
        return str(iso or '')


def hash_pw(pw, salt):
    return hashlib.pbkdf2_hmac('sha256', pw.encode(), salt.encode(), 120_000).hex()


def check_password(pw):
    if len(pw) < 8:
        return "Le mot de passe doit contenir au moins 8 caractères."
    if pw.lower() in WEAK:
        return "Mot de passe trop courant."
    if not (re.search(r'[A-Za-z]', pw) and re.search(r'\d', pw)):
        return "Le mot de passe doit contenir des lettres et des chiffres."
    return ""


def annee_courante():
    t = date.today()
    return str(t.year)


def year_path(label):
    return os.path.join(ANNEES_DIR, f"boutique_{label}.db")


def list_years():
    try:
        return sorted(f[9:-3] for f in os.listdir(ANNEES_DIR) if f.startswith('boutique_') and f.endswith('.db'))
    except OSError:
        return []


def plan_payments(total, paiements):
    """Paiement mixte. paiements : [(mode, montant, reference)] dans l'ordre de saisie.
    Retourne (reglements, rendu, erreur). Si la somme dépasse le total, l'excédent est rendu en monnaie :
    il est déduit des paiements en espèces (en commençant par le dernier) et refusé s'il n'y a pas assez d'espèces.
    Si la somme est inférieure au total, le reste sera enregistré à crédit par vendre()."""
    regs = []
    for mode, montant, ref in paiements:
        if not _fini(montant) or montant < 0:
            return [], 0, "Montant de paiement invalide."
        if montant > 0:
            regs.append([mode, float(montant), ref or ""])
    somme = sum(r[1] for r in regs)
    exces = round(somme - total, 2)
    rendu = 0
    if exces > 0.001:
        if exces > sum(r[1] for r in regs if r[0] == "Espèces") + 0.001:
            return [], 0, "Le trop-perçu ne peut être rendu qu'en espèces : réduisez les paiements par mobile money ou carte."
        reste = exces
        for r in reversed(regs):
            if r[0] == "Espèces" and reste > 0:
                d = min(r[1], reste)
                r[1] -= d
                reste -= d
        rendu = exces
        regs = [r for r in regs if r[1] > 0.001]
    return [tuple(r) for r in regs], rendu, ""


def _fini(*valeurs):
    """Vrai si toutes les valeurs sont des nombres finis (rejette nan / inf qui passeraient les comparaisons)."""
    try:
        return all(math.isfinite(float(v)) for v in valeurs)
    except (TypeError, ValueError):
        return False


def _ean_ok(code):
    """Contrôle d'un code-barres EAN-13 / EAN-8 / UPC-A (chiffre de contrôle). Autres codes (internes) : acceptés."""
    if not code.isdigit() or len(code) not in (8, 12, 13):
        return True
    d = [int(c) for c in code]
    s = sum(x * (3 if i % 2 == 0 else 1) for i, x in enumerate(reversed(d[:-1])))
    return (10 - s % 10) % 10 == d[-1]


# ---------------------------------------------------------------- base de données
class Database:
    def __init__(self, path=None):
        path = path or year_path(annee_courante())
        self.path = path
        self.actor, self.actor_role, self.auth_msg = '-', '', ''
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self.conn = sqlite3.connect(path, timeout=30, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        try:
            self.conn.create_function('norm', 1, norm, deterministic=True)
        except (TypeError, sqlite3.NotSupportedError):
            self.conn.create_function('norm', 1, norm)
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA busy_timeout = 30000")
        self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.execute("PRAGMA synchronous = NORMAL")
        self.create_tables()
        self.seed()
        self._flag_defaults()

    # ------------------------------------------------------------ schéma
    def create_tables(self):
        self.conn.executescript('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT UNIQUE NOT NULL, salt TEXT NOT NULL,
            password_hash TEXT NOT NULL, role TEXT NOT NULL, full_name TEXT, last_login TEXT,
            echecs INTEGER DEFAULT 0, bloque_jusqua TEXT, must_change INTEGER DEFAULT 0);
        CREATE TABLE IF NOT EXISTS categories (
            id INTEGER PRIMARY KEY AUTOINCREMENT, nom TEXT UNIQUE NOT NULL, famille TEXT DEFAULT 'autre');
        CREATE TABLE IF NOT EXISTS fournisseurs (
            id INTEGER PRIMARY KEY AUTOINCREMENT, nom TEXT UNIQUE NOT NULL, contact TEXT, telephone TEXT, adresse TEXT);
        CREATE TABLE IF NOT EXISTS articles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT UNIQUE NOT NULL,                 -- référence interne (générée) ou code-barres
            designation TEXT NOT NULL,
            categorie_id INTEGER NOT NULL REFERENCES categories(id),
            auteur TEXT, editeur TEXT, isbn TEXT,      -- livres
            niveau TEXT,                               -- classe / niveau scolaire (ex. 6ème, CM2, Tle)
            unite TEXT DEFAULT 'pièce',
            prix_achat REAL DEFAULT 0 CHECK(prix_achat >= 0),
            prix_vente REAL NOT NULL CHECK(prix_vente >= 0),
            stock REAL DEFAULT 0,
            seuil_alerte REAL DEFAULT 5,
            fournisseur_id INTEGER REFERENCES fournisseurs(id),
            emplacement TEXT, actif INTEGER DEFAULT 1, cree_le TEXT DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE IF NOT EXISTS clients (
            id INTEGER PRIMARY KEY AUTOINCREMENT, nom TEXT NOT NULL, telephone TEXT, adresse TEXT,
            type TEXT DEFAULT 'particulier',           -- particulier / ecole / entreprise
            plafond_credit REAL DEFAULT 0, remise REAL DEFAULT 0 CHECK(remise >= 0 AND remise <= 100));
        CREATE TABLE IF NOT EXISTS ventes (
            id INTEGER PRIMARY KEY AUTOINCREMENT, facture_no TEXT UNIQUE NOT NULL, date TEXT NOT NULL,
            client_id INTEGER REFERENCES clients(id), vendeur TEXT,
            sous_total REAL, remise REAL DEFAULT 0, tva REAL DEFAULT 0, total REAL NOT NULL,
            annule INTEGER DEFAULT 0, annule_par TEXT, annule_date TEXT, annule_motif TEXT);
        CREATE TABLE IF NOT EXISTS vente_lignes (
            id INTEGER PRIMARY KEY AUTOINCREMENT, vente_id INTEGER NOT NULL REFERENCES ventes(id),
            article_id INTEGER NOT NULL REFERENCES articles(id), designation TEXT,
            quantite REAL NOT NULL CHECK(quantite > 0), prix_unitaire REAL NOT NULL, prix_achat REAL DEFAULT 0,
            remise_pct REAL DEFAULT 0, total REAL NOT NULL, qte_retournee REAL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS reglements (          -- paiements (ventes comptant, tranches de crédit)
            id INTEGER PRIMARY KEY AUTOINCREMENT, recu_no TEXT UNIQUE NOT NULL, date TEXT NOT NULL,
            vente_id INTEGER REFERENCES ventes(id), client_id INTEGER REFERENCES clients(id),
            montant REAL NOT NULL CHECK(montant > 0), mode TEXT NOT NULL, reference TEXT, caissier TEXT,
            annule INTEGER DEFAULT 0, annule_motif TEXT);
        CREATE TABLE IF NOT EXISTS retours (
            id INTEGER PRIMARY KEY AUTOINCREMENT, date TEXT NOT NULL, vente_id INTEGER NOT NULL REFERENCES ventes(id),
            ligne_id INTEGER NOT NULL REFERENCES vente_lignes(id), quantite REAL NOT NULL, montant REAL NOT NULL,
            motif TEXT, remise_en_stock INTEGER DEFAULT 1, par TEXT);
        CREATE TABLE IF NOT EXISTS achats (
            id INTEGER PRIMARY KEY AUTOINCREMENT, bon_no TEXT UNIQUE NOT NULL, date TEXT NOT NULL,
            fournisseur_id INTEGER REFERENCES fournisseurs(id), reference_fournisseur TEXT,
            total REAL NOT NULL, paye REAL DEFAULT 0, par TEXT, annule INTEGER DEFAULT 0);
        CREATE TABLE IF NOT EXISTS achat_lignes (
            id INTEGER PRIMARY KEY AUTOINCREMENT, achat_id INTEGER NOT NULL REFERENCES achats(id),
            article_id INTEGER NOT NULL REFERENCES articles(id), quantite REAL NOT NULL CHECK(quantite > 0),
            prix_achat REAL NOT NULL, total REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS paiements_fournisseurs (
            id INTEGER PRIMARY KEY AUTOINCREMENT, date TEXT NOT NULL, achat_id INTEGER NOT NULL REFERENCES achats(id),
            montant REAL NOT NULL CHECK(montant > 0), mode TEXT, par TEXT);
        CREATE TABLE IF NOT EXISTS mouvements (          -- historique complet du stock
            id INTEGER PRIMARY KEY AUTOINCREMENT, date TEXT NOT NULL, article_id INTEGER NOT NULL REFERENCES articles(id),
            type TEXT NOT NULL,                          -- vente, achat, retour, annulation, ajustement, inventaire, perte
            quantite REAL NOT NULL,                      -- + entrée / - sortie
            stock_apres REAL, reference TEXT, motif TEXT, par TEXT);
        CREATE TABLE IF NOT EXISTS depenses (
            id INTEGER PRIMARY KEY AUTOINCREMENT, date TEXT NOT NULL, libelle TEXT NOT NULL,
            montant REAL NOT NULL CHECK(montant > 0), categorie TEXT, par TEXT);
        CREATE TABLE IF NOT EXISTS clotures (
            id INTEGER PRIMARY KEY AUTOINCREMENT, jour TEXT NOT NULL, caissier TEXT NOT NULL, nb INTEGER,
            total_especes REAL, total_autres REAL, depenses REAL DEFAULT 0, attendu REAL, compte REAL, ecart REAL,
            note TEXT, cree_par TEXT, cree_le TEXT, rouverte INTEGER DEFAULT 0, rouverte_par TEXT, rouverte_motif TEXT);
        CREATE TABLE IF NOT EXISTS packs (               -- listes de fournitures / packs scolaires
            id INTEGER PRIMARY KEY AUTOINCREMENT, nom TEXT UNIQUE NOT NULL, niveau TEXT);
        CREATE TABLE IF NOT EXISTS pack_lignes (
            id INTEGER PRIMARY KEY AUTOINCREMENT, pack_id INTEGER NOT NULL REFERENCES packs(id) ON DELETE CASCADE,
            article_id INTEGER NOT NULL REFERENCES articles(id), quantite REAL NOT NULL CHECK(quantite > 0),
            UNIQUE(pack_id, article_id));
        CREATE TABLE IF NOT EXISTS audit (
            id INTEGER PRIMARY KEY AUTOINCREMENT, date TEXT NOT NULL, utilisateur TEXT, action TEXT NOT NULL,
            details TEXT, hash TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS idx_art_cat ON articles(categorie_id);
        CREATE INDEX IF NOT EXISTS idx_art_des ON articles(designation);
        CREATE INDEX IF NOT EXISTS idx_art_isbn ON articles(isbn);
        CREATE INDEX IF NOT EXISTS idx_vente_date ON ventes(date);
        CREATE INDEX IF NOT EXISTS idx_vl_vente ON vente_lignes(vente_id);
        CREATE INDEX IF NOT EXISTS idx_reg_vente ON reglements(vente_id);
        CREATE INDEX IF NOT EXISTS idx_reg_client ON reglements(client_id);
        CREATE INDEX IF NOT EXISTS idx_mvt_art ON mouvements(article_id, date);
        CREATE UNIQUE INDEX IF NOT EXISTS idx_clot_actif ON clotures(jour, caissier) WHERE rouverte=0;
        ''')
        self.conn.commit()

    def seed(self):
        if not self.conn.execute("SELECT 1 FROM users").fetchone():
            for u, p, r in [('admin', 'admin123', 'admin'), ('gerant', '1234', 'gerant'), ('vendeur', '1234', 'vendeur')]:
                self.add_user(u, p, r, ROLES[r])
        if not self.conn.execute("SELECT 1 FROM categories").fetchone():
            self.conn.executemany("INSERT INTO categories (nom, famille) VALUES (?,?)", CATEGORIES)
            self.conn.commit()

    def _flag_defaults(self):
        for u in self.conn.execute("SELECT id, salt, password_hash FROM users WHERE must_change=0").fetchall():
            if any(hash_pw(p, u['salt']) == u['password_hash'] for p in ('admin123', '1234')):
                self.conn.execute("UPDATE users SET must_change=1 WHERE id=?", (u['id'],))
        self.conn.commit()

    # ------------------------------------------------------------ audit (chaîne de hachage)
    def log(self, action, details="", user=None):
        user = user or self.actor
        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        prev = self.conn.execute("SELECT hash FROM audit ORDER BY id DESC LIMIT 1").fetchone()
        h = hashlib.sha256(f"{prev['hash'] if prev else ''}|{now}|{user}|{action}|{details}".encode()).hexdigest()
        self.conn.execute("INSERT INTO audit (date, utilisateur, action, details, hash) VALUES (?,?,?,?,?)",
                          (now, user, action, details, h))
        self.conn.commit()

    def get_audit(self, term="", d1=None, d2=None, limit=500):
        q, a = "SELECT * FROM audit WHERE 1=1", []
        if term:
            q += " AND (norm(action) LIKE ? OR norm(details) LIKE ? OR norm(utilisateur) LIKE ?)"
            a += [f"%{norm(term)}%"] * 3
        if d1:
            q += " AND substr(date,1,10) >= ?"; a.append(d1)
        if d2:
            q += " AND substr(date,1,10) <= ?"; a.append(d2)
        return self.conn.execute(q + " ORDER BY id DESC LIMIT ?", a + [limit]).fetchall()

    def verify_audit(self):
        prev, n = '', 0
        for r in self.conn.execute("SELECT * FROM audit ORDER BY id"):
            h = hashlib.sha256(f"{prev}|{r['date']}|{r['utilisateur']}|{r['action']}|{r['details']}".encode()).hexdigest()
            if h != r['hash']:
                return False, f"Altération détectée à l'entrée n° {r['id']}."
            prev, n = h, n + 1
        return True, f"Journal intact ({n} entrées)."

    def is_healthy(self):
        return self.conn.execute("PRAGMA integrity_check").fetchone()[0] == 'ok'

    # ------------------------------------------------------------ utilisateurs
    def add_user(self, username, password, role, full_name, must_change=False):
        if role not in ROLES:
            raise ValueError("Rôle inconnu")
        salt = secrets.token_hex(16)
        self.conn.execute("INSERT INTO users (username, salt, password_hash, role, full_name, must_change) VALUES (?,?,?,?,?,?)",
                          (username, salt, hash_pw(password, salt), role, full_name, int(must_change)))
        self.conn.commit()
        self.log("Utilisateur créé", f"{username} ({role})")

    def authenticate(self, username, password):
        self.auth_msg = ""
        r = self.conn.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
        now = datetime.now()
        stamp = now.strftime('%Y-%m-%d %H:%M:%S')
        if not r:
            self.log("Échec de connexion", "identifiant inconnu", user=username)
            return None
        if r['bloque_jusqua'] and r['bloque_jusqua'] > stamp:
            self.auth_msg = f"Compte bloqué jusqu'à {r['bloque_jusqua'][11:16]} (trop de tentatives)."
            return None
        if secrets.compare_digest(r['password_hash'], hash_pw(password, r['salt'])):
            self.conn.execute("UPDATE users SET last_login=?, echecs=0, bloque_jusqua=NULL WHERE id=?", (stamp, r['id']))
            self.conn.commit()
            self.log("Connexion", user=username)
            return dict(id=r['id'], username=r['username'], role=r['role'], full_name=r['full_name'],
                        must_change=bool(r['must_change']))
        n = (r['echecs'] or 0) + 1
        if n >= MAX_ECHECS:
            until = (now + timedelta(minutes=BLOCAGE_MIN)).strftime('%Y-%m-%d %H:%M:%S')
            self.conn.execute("UPDATE users SET echecs=0, bloque_jusqua=? WHERE id=?", (until, r['id']))
            self.auth_msg = f"Trop de tentatives : compte bloqué pendant {BLOCAGE_MIN} minutes."
        else:
            self.conn.execute("UPDATE users SET echecs=? WHERE id=?", (n, r['id']))
            self.auth_msg = f"Mot de passe incorrect ({MAX_ECHECS - n} essai(s) restant(s))."
        self.conn.commit()
        self.log("Échec de connexion", f"tentative {n}", user=username)
        return None

    def change_password(self, username, old, new):
        r = self.conn.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
        if not r or hash_pw(old, r['salt']) != r['password_hash']:
            return False, "Ancien mot de passe incorrect."
        msg = check_password(new)
        if msg:
            return False, msg
        salt = secrets.token_hex(16)
        self.conn.execute("UPDATE users SET salt=?, password_hash=?, must_change=0 WHERE id=?", (salt, hash_pw(new, salt), r['id']))
        self.conn.commit()
        self.log("Mot de passe changé", username, user=username)
        return True, "Mot de passe modifié."

    def reset_password(self, uid, nouveau):
        """Réinitialisation par l'administrateur : l'utilisateur devra choisir un nouveau mot de passe à sa prochaine connexion."""
        self._need('admin')
        r = self.conn.execute("SELECT id, username FROM users WHERE id=?", (uid,)).fetchone()
        if not r:
            return False, "Utilisateur introuvable."
        msg = check_password(nouveau)
        if msg:
            return False, msg
        salt = secrets.token_hex(16)
        self.conn.execute("UPDATE users SET salt=?, password_hash=?, must_change=1 WHERE id=?", (salt, hash_pw(nouveau, salt), uid))
        self.conn.commit()
        self.log("Mot de passe réinitialisé", r['username'])
        return True, "Mot de passe réinitialisé : il devra être changé à la prochaine connexion."

    def update_user(self, uid, role, full_name):
        self._need('admin')
        if role not in ROLES:
            return False, "Rôle inconnu."
        r = self.conn.execute("SELECT username, role FROM users WHERE id=?", (uid,)).fetchone()
        if not r:
            return False, "Utilisateur introuvable."
        if r['role'] == 'admin' and role != 'admin' and self.conn.execute("SELECT COUNT(*) FROM users WHERE role='admin'").fetchone()[0] <= 1:
            return False, "Impossible de retirer le droit du dernier administrateur."
        self.conn.execute("UPDATE users SET role=?, full_name=? WHERE id=?", (role, (full_name or '').strip() or r['username'], uid))
        self.conn.commit()
        self.log("Utilisateur modifié", f"{r['username']} ({role})")
        return True, "Utilisateur modifié."

    def get_users(self):
        return self.conn.execute("SELECT id, username, role, full_name, last_login FROM users ORDER BY id").fetchall()

    def delete_user(self, uid):
        r = self.conn.execute("SELECT username, role FROM users WHERE id=?", (uid,)).fetchone()
        if not r:
            return False, "Introuvable."
        if r['role'] == 'admin' and self.conn.execute("SELECT COUNT(*) FROM users WHERE role='admin'").fetchone()[0] <= 1:
            return False, "Impossible de supprimer le dernier administrateur."
        self.conn.execute("DELETE FROM users WHERE id=?", (uid,))
        self.conn.commit()
        self.log("Utilisateur supprimé", r['username'])
        return True, "Utilisateur supprimé."

    def _need(self, *roles):
        if self.actor_role and self.actor_role not in roles:
            raise PermissionError("Droits insuffisants pour cette opération.")

    # ------------------------------------------------------------ catégories / fournisseurs / clients
    def get_categories(self):
        return self.conn.execute("SELECT * FROM categories ORDER BY famille, nom").fetchall()

    def save_categorie(self, cid, nom, famille):
        self._need('admin', 'gerant')
        nom = nom.strip()
        if not nom:
            return False, "Nom obligatoire."
        try:
            if cid:
                self.conn.execute("UPDATE categories SET nom=?, famille=? WHERE id=?", (nom, famille, cid))
            else:
                self.conn.execute("INSERT INTO categories (nom, famille) VALUES (?,?)", (nom, famille))
        except sqlite3.IntegrityError:
            return False, "Cette catégorie existe déjà."
        self.conn.commit()
        self.log("Catégorie enregistrée", nom)
        return True, "Catégorie enregistrée."

    def get_fournisseurs(self):
        return self.conn.execute("""SELECT f.*, COALESCE((SELECT SUM(a.total - a.paye) FROM achats a
                                    WHERE a.fournisseur_id=f.id AND a.annule=0),0) AS dette
                                    FROM fournisseurs f ORDER BY f.nom""").fetchall()

    def save_fournisseur(self, fid, nom, contact="", tel="", adresse=""):
        self._need('admin', 'gerant', 'magasinier')
        nom = nom.strip()
        if not nom:
            return False, "Nom obligatoire."
        try:
            if fid:
                self.conn.execute("UPDATE fournisseurs SET nom=?, contact=?, telephone=?, adresse=? WHERE id=?", (nom, contact, tel, adresse, fid))
            else:
                self.conn.execute("INSERT INTO fournisseurs (nom, contact, telephone, adresse) VALUES (?,?,?,?)", (nom, contact, tel, adresse))
        except sqlite3.IntegrityError:
            return False, "Ce fournisseur existe déjà."
        self.conn.commit()
        self.log("Fournisseur enregistré", nom)
        return True, "Fournisseur enregistré."

    def get_clients(self, term=""):
        q = """SELECT c.*, (COALESCE((SELECT SUM(v.total) FROM ventes v WHERE v.client_id=c.id AND v.annule=0),0)
                           - COALESCE((SELECT SUM(r.montant) FROM reglements r JOIN ventes v ON v.id=r.vente_id
                                       WHERE v.client_id=c.id AND v.annule=0 AND r.annule=0),0)) AS solde
               FROM clients c WHERE 1=1"""
        a = []
        if term:
            q += " AND (norm(c.nom) LIKE ? OR c.telephone LIKE ?)"
            a += [f"%{norm(term)}%", f"%{term}%"]
        return self.conn.execute(q + " ORDER BY c.nom", a).fetchall()

    def save_client(self, cid, nom, tel="", adresse="", type_="particulier", plafond=0, remise=0):
        nom = nom.strip()
        if not nom:
            return False, "Nom obligatoire."
        if not (0 <= remise <= 100) or plafond < 0:
            return False, "Remise ou plafond invalide."
        if cid:
            self.conn.execute("UPDATE clients SET nom=?, telephone=?, adresse=?, type=?, plafond_credit=?, remise=? WHERE id=?",
                              (nom, tel, adresse, type_, plafond, remise, cid))
        else:
            cur = self.conn.execute("INSERT INTO clients (nom, telephone, adresse, type, plafond_credit, remise) VALUES (?,?,?,?,?,?)",
                                    (nom, tel, adresse, type_, plafond, remise))
            self.last_client_id = cur.lastrowid
        self.conn.commit()
        self.log("Client enregistré", nom)
        return True, "Client enregistré."

    # ------------------------------------------------------------ articles
    def next_code(self, categorie_id):
        r = self.conn.execute("SELECT famille FROM categories WHERE id=?", (categorie_id,)).fetchone()
        pre = {'livre': 'LIV', 'fourniture': 'FOU', 'bureau': 'BUR'}.get(r['famille'] if r else '', 'ART')
        n = self.conn.execute("SELECT COUNT(*) FROM articles WHERE code LIKE ?", (pre + '-%',)).fetchone()[0] + 1
        while self.conn.execute("SELECT 1 FROM articles WHERE code=?", (f"{pre}-{n:05d}",)).fetchone():
            n += 1
        return f"{pre}-{n:05d}"

    def save_article(self, aid, designation, categorie_id, prix_vente, prix_achat=0, code="", auteur="", editeur="",
                     isbn="", niveau="", unite="pièce", seuil=5, fournisseur_id=None, emplacement="", stock_initial=0):
        self._need('admin', 'gerant', 'magasinier')
        designation = designation.strip()
        if not designation:
            return False, "Désignation obligatoire."
        if prix_vente < 0 or prix_achat < 0:
            return False, "Prix invalide."
        if prix_achat and prix_vente < prix_achat:
            pass  # autorisé (soldes) : signalé dans l'interface, pas bloquant
        code, isbn = (code or "").strip(), (isbn or "").replace('-', '').replace(' ', '')
        if isbn and not _ean_ok(isbn):
            return False, "ISBN / code-barres invalide (chiffre de contrôle incorrect)."
        if not code:
            code = isbn or self.next_code(categorie_id)
        dup = self.conn.execute("SELECT id FROM articles WHERE code=? AND id IS NOT ?", (code, aid)).fetchone()
        if dup:
            return False, f"Le code « {code} » est déjà utilisé."
        if isbn and self.conn.execute("SELECT 1 FROM articles WHERE isbn=? AND id IS NOT ?", (isbn, aid)).fetchone():
            return False, "Cet ISBN existe déjà dans le catalogue."
        vals = (code, designation, categorie_id, auteur, editeur, isbn or None, niveau, unite, prix_achat, prix_vente,
                seuil, fournisseur_id, emplacement)
        if aid:
            self.conn.execute("""UPDATE articles SET code=?, designation=?, categorie_id=?, auteur=?, editeur=?, isbn=?, niveau=?,
                                 unite=?, prix_achat=?, prix_vente=?, seuil_alerte=?, fournisseur_id=?, emplacement=? WHERE id=?""",
                              vals + (aid,))
            self.log("Article modifié", f"{code} - {designation} - vente {fcfa(prix_vente)}")
        else:
            cur = self.conn.execute("""INSERT INTO articles (code, designation, categorie_id, auteur, editeur, isbn, niveau, unite,
                                       prix_achat, prix_vente, seuil_alerte, fournisseur_id, emplacement) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""", vals)
            aid = cur.lastrowid
            self.conn.commit()
            self.log("Article créé", f"{code} - {designation}")
            if stock_initial:
                self._mouvement(aid, 'inventaire', stock_initial, "Stock initial", "Création de l'article")
        self.last_article_id = aid
        self.conn.commit()
        return True, "Article enregistré."

    def archive_article(self, aid, actif=False):
        self._need('admin', 'gerant')
        self.conn.execute("UPDATE articles SET actif=? WHERE id=?", (int(actif), aid))
        self.conn.commit()
        self.log("Article " + ("réactivé" if actif else "archivé"), str(aid))

    def get_article(self, aid):
        return self.conn.execute("""SELECT a.*, c.nom AS categorie, c.famille FROM articles a
                                    JOIN categories c ON c.id=a.categorie_id WHERE a.id=?""", (aid,)).fetchone()

    def find_code(self, code):
        """Recherche exacte (lecteur de code-barres) : code interne ou ISBN."""
        code = (code or "").strip()
        return self.conn.execute("""SELECT a.*, c.nom AS categorie FROM articles a JOIN categories c ON c.id=a.categorie_id
                                    WHERE a.actif=1 AND (a.code=? OR a.isbn=?)""", (code, code.replace('-', '').replace(' ', ''))).fetchone()

    def search_articles(self, term="", categorie_id=None, famille=None, niveau="", stock_filtre="", actifs=True, limit=500):
        """stock_filtre : '' tout | 'bas' sous le seuil | 'rupture' stock<=0"""
        q = """SELECT a.*, c.nom AS categorie, c.famille, f.nom AS fournisseur,
                      a.stock * a.prix_achat AS valeur_achat, a.stock * a.prix_vente AS valeur_vente
               FROM articles a JOIN categories c ON c.id=a.categorie_id LEFT JOIN fournisseurs f ON f.id=a.fournisseur_id WHERE 1=1"""
        a = []
        if actifs:
            q += " AND a.actif=1"
        for w in norm(term).split():
            q += " AND (norm(a.designation) LIKE ? OR norm(a.auteur) LIKE ? OR norm(a.code) LIKE ? OR a.isbn LIKE ? OR norm(a.editeur) LIKE ?)"
            a += [f"%{w}%", f"%{w}%", f"%{w}%", f"%{w.replace('-', '')}%", f"%{w}%"]
        if categorie_id:
            q += " AND a.categorie_id=?"; a.append(categorie_id)
        if famille:
            q += " AND c.famille=?"; a.append(famille)
        if niveau:
            q += " AND norm(a.niveau) LIKE ?"; a.append(f"%{norm(niveau)}%")
        if stock_filtre == 'bas':
            q += " AND a.stock <= a.seuil_alerte AND a.stock > 0"
        elif stock_filtre == 'rupture':
            q += " AND a.stock <= 0"
        return self.conn.execute(q + " ORDER BY a.designation LIMIT ?", a + [limit]).fetchall()

    # ------------------------------------------------------------ stock
    def _mouvement(self, aid, type_, qte, ref="", motif="", par=None):
        """Applique un mouvement (+ entrée / - sortie) ; refuse un stock négatif. Ne valide pas la transaction."""
        r = self.conn.execute("SELECT stock FROM articles WHERE id=?", (aid,)).fetchone()
        nouveau = (r['stock'] or 0) + qte
        if nouveau < -0.0001:
            raise ValueError("Stock insuffisant")
        self.conn.execute("UPDATE articles SET stock=? WHERE id=?", (nouveau, aid))
        cur = self.conn.execute("INSERT INTO mouvements (date, article_id, type, quantite, stock_apres, reference, motif, par) VALUES (?,?,?,?,?,?,?,?)",
                                (datetime.now().strftime('%Y-%m-%d %H:%M:%S'), aid, type_, qte, nouveau, ref, motif, par or self.actor))
        self.last_mouvement_id = cur.lastrowid

    def ajuster_stock(self, aid, delta, type_, motif):
        """Ajustement manuel (casse, perte, vol, don, correction) : motif obligatoire."""
        self._need('admin', 'gerant', 'magasinier')
        if type_ not in ('ajustement', 'perte'):
            return False, "Type invalide."
        if not delta:
            return False, "Quantité nulle."
        if len((motif or '').strip()) < 4:
            return False, "Un motif d'au moins 4 caractères est obligatoire."
        try:
            self._mouvement(aid, type_, delta, "", motif.strip())
        except ValueError:
            self.conn.rollback()
            return False, "Le stock ne peut pas devenir négatif."
        self.conn.commit()
        a = self.get_article(aid)
        self.log("Stock " + type_, f"{a['code']} {a['designation']} : {delta:+g} ({motif.strip()})")
        return True, "Stock ajusté."

    def inventaire(self, comptages, motif="Inventaire physique"):
        """comptages : {article_id: quantité comptée}. Crée les mouvements d'écart. Renvoie la liste des écarts."""
        self._need('admin', 'gerant', 'magasinier')
        ecarts = []
        for aid, compte in comptages.items():
            a = self.get_article(aid)
            if not a or compte < 0:
                continue
            d = compte - (a['stock'] or 0)
            if abs(d) > 0.0001:
                self._mouvement(aid, 'inventaire', d, "", motif)
                ecarts.append((a['code'], a['designation'], a['stock'], compte, d, d * a['prix_achat']))
        self.conn.commit()
        self.log("Inventaire", f"{len(comptages)} article(s) comptés, {len(ecarts)} écart(s), valeur {fcfa(sum(e[5] for e in ecarts))}")
        return ecarts

    def get_mouvements(self, aid=None, d1=None, d2=None, limit=500):
        q = "SELECT m.*, a.code, a.designation FROM mouvements m JOIN articles a ON a.id=m.article_id WHERE 1=1"
        p = []
        if aid:
            q += " AND m.article_id=?"; p.append(aid)
        if d1:
            q += " AND substr(m.date,1,10)>=?"; p.append(d1)
        if d2:
            q += " AND substr(m.date,1,10)<=?"; p.append(d2)
        return self.conn.execute(q + " ORDER BY m.id DESC LIMIT ?", p + [limit]).fetchall()

    def alertes_stock(self):
        return self.search_articles(stock_filtre='bas') + self.search_articles(stock_filtre='rupture')

    def valeur_stock(self):
        r = self.conn.execute("SELECT COALESCE(SUM(stock*prix_achat),0) a, COALESCE(SUM(stock*prix_vente),0) v, COUNT(*) n FROM articles WHERE actif=1 AND stock>0").fetchone()
        return dict(achat=r['a'], vente=r['v'], articles=r['n'], marge_latente=r['v'] - r['a'])

    # ------------------------------------------------------------ ventes
    def _numero(self, table, col, prefixe):
        n = self.conn.execute(f"SELECT COALESCE(MAX(id),0)+1 FROM {table}").fetchone()[0]
        return f"{prefixe}{date.today().year}-{n:06d}"

    def _paye_vente(self, vid):
        return self.conn.execute("SELECT COALESCE(SUM(montant),0) FROM reglements WHERE vente_id=? AND annule=0", (vid,)).fetchone()[0]

    def vendre(self, lignes, reglements, client_id=None, remise_globale=0.0, vendeur=None):
        """
        lignes      : [(article_id, quantite, prix_unitaire|None, remise_pct)]  (prix None = prix catalogue)
        reglements  : [(mode, montant, reference)]  ; la somme peut être < total => reste à crédit (client obligatoire)
        remise_globale : en % sur le sous-total.
        Tout est enregistré ou rien (transaction). Retourne (ok, msg, vente_id).
        """
        self._need('admin', 'gerant', 'vendeur')
        vendeur = vendeur or self.actor
        if not lignes:
            return False, "Le panier est vide.", None
        cfg = get_config()
        if self.actor_role == 'vendeur' and (remise_globale > cfg['seuil_remise_vendeur'] or
                                             any(l[3] > cfg['seuil_remise_vendeur'] for l in lignes)):
            return False, f"Remise limitée à {cfg['seuil_remise_vendeur']:g} % pour un vendeur : demandez au gérant.", None
        if not _fini(remise_globale) or not (0 <= remise_globale <= 100):
            return False, "Remise invalide.", None
        if self.cloture_active(date.today().isoformat(), vendeur):
            return False, "Cette caisse est clôturée pour aujourd'hui (le gérant peut la rouvrir).", None
        try:
            vus, det, sous = set(), [], 0.0
            for aid, qte, pu, rem in lignes:
                if aid in vus:
                    raise ValueError("Un même article apparaît deux fois dans le panier.")
                vus.add(aid)
                a = self.get_article(aid)
                if not a or not a['actif']:
                    raise ValueError("Article introuvable ou archivé.")
                if not _fini(qte, pu if pu is not None else 0, rem) or qte <= 0:
                    raise ValueError(f"Quantité invalide pour « {a['designation']} ».")
                if a['stock'] < qte - 0.0001:
                    raise ValueError(f"Stock insuffisant pour « {a['designation']} » (disponible : {a['stock']:g}).")
                pu = a['prix_vente'] if pu is None else pu
                if pu < 0 or not (0 <= rem <= 100):
                    raise ValueError("Prix ou remise invalide.")
                if self.actor_role == 'vendeur' and pu < a['prix_vente'] - 0.001:
                    raise ValueError("Un vendeur ne peut pas baisser le prix : utilisez la remise (dans la limite autorisée).")
                tot = round(qte * pu * (1 - rem / 100))
                det.append((aid, a['designation'], qte, pu, a['prix_achat'], rem, tot))
                sous += tot
            remise = round(sous * remise_globale / 100)
            tva = round((sous - remise) * cfg['tva_taux'] / 100)
            total = sous - remise + tva
            paye = sum(m for _, m, _ in reglements)
            if any(m <= 0 for _, m, _ in reglements):
                raise ValueError("Montant de règlement invalide.")
            if paye > total + 0.001:
                raise ValueError(f"Le montant encaissé ({fcfa(paye)}) dépasse le total ({fcfa(total)}).")
            reste = total - paye
            if reste > 0.001:
                if not client_id:
                    raise ValueError("Vente à crédit : sélectionnez un client.")
                c = self.conn.execute("SELECT * FROM clients WHERE id=?", (client_id,)).fetchone()
                solde = next(x['solde'] for x in self.get_clients() if x['id'] == client_id)
                if solde + reste > c['plafond_credit'] + 0.001:
                    raise ValueError(f"Plafond de crédit dépassé pour {c['nom']} (plafond {fcfa(c['plafond_credit'])}, "
                                     f"dette actuelle {fcfa(solde)}).")
            now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            fac = self._numero('ventes', 'facture_no', 'FAC')
            cur = self.conn.execute("""INSERT INTO ventes (facture_no, date, client_id, vendeur, sous_total, remise, tva, total)
                                       VALUES (?,?,?,?,?,?,?,?)""", (fac, now, client_id, vendeur, sous, remise, tva, total))
            vid = cur.lastrowid
            for aid, des, qte, pu, pa, rem, tot in det:
                self.conn.execute("""INSERT INTO vente_lignes (vente_id, article_id, designation, quantite, prix_unitaire, prix_achat, remise_pct, total)
                                     VALUES (?,?,?,?,?,?,?,?)""", (vid, aid, des, qte, pu, pa, rem, tot))
                self._mouvement(aid, 'vente', -qte, fac, "", vendeur)
            for mode, m, ref in reglements:
                self._ajouter_reglement(vid, client_id, m, mode, ref, vendeur, now)
        except (ValueError, sqlite3.Error) as e:
            self.conn.rollback()
            return False, str(e), None
        self.conn.commit()
        self.log("Vente", f"{fac} - {len(det)} ligne(s) - {fcfa(total)} - encaissé {fcfa(paye)}"
                 + (f" - crédit {fcfa(reste)}" if reste > 0.001 else ""))
        return True, "Vente enregistrée.", vid

    def _ajouter_reglement(self, vid, client_id, montant, mode, ref, caissier, now=None):
        if mode not in MODES or mode == "Crédit client":
            raise ValueError("Mode de paiement invalide.")
        ref = re.sub(r"\s+", " ", (ref or "").strip())
        if mode == "Espèces":
            ref = ""
        else:
            if len(ref) < 4:
                raise ValueError(f"{REF_LABELS.get(mode, 'Référence')} obligatoire pour un paiement par {mode} (4 caractères minimum).")
            if self.conn.execute("SELECT 1 FROM reglements WHERE annule=0 AND UPPER(REPLACE(reference,' ',''))=UPPER(REPLACE(?,' ',''))", (ref,)).fetchone():
                raise ValueError("Cette référence de paiement a déjà été utilisée.")
        n = self.conn.execute("SELECT COALESCE(MAX(id),0)+1 FROM reglements").fetchone()[0]
        recu = f"REC{date.today().year}-{n:06d}"
        cur = self.conn.execute("INSERT INTO reglements (recu_no, date, vente_id, client_id, montant, mode, reference, caissier) VALUES (?,?,?,?,?,?,?,?)",
                                (recu, now or datetime.now().strftime('%Y-%m-%d %H:%M:%S'), vid, client_id, montant, mode, ref or None, caissier))
        return cur.lastrowid

    def encaisser_credit(self, vente_id, montant, mode, reference="", caissier=None):
        """Règlement d'une dette client (paiement échelonné)."""
        self._need('admin', 'gerant', 'vendeur')
        caissier = caissier or self.actor
        v = self.get_vente(vente_id)
        if not v or v['annule']:
            return False, "Facture introuvable ou annulée.", None
        reste = v['total'] - self._paye_vente(vente_id)
        if montant <= 0 or montant > reste + 0.001:
            return False, f"Montant invalide (reste dû : {fcfa(reste)}).", None
        if self.cloture_active(date.today().isoformat(), caissier):
            return False, "Cette caisse est clôturée pour aujourd'hui.", None
        try:
            rid = self._ajouter_reglement(vente_id, v['client_id'], montant, mode, reference, caissier)
        except ValueError as e:
            self.conn.rollback()
            return False, str(e), None
        self.conn.commit()
        self.log("Règlement de dette", f"{v['facture_no']} - {fcfa(montant)} ({mode})")
        return True, "Règlement enregistré.", rid

    def get_reglement(self, rid):
        """Un règlement avec sa facture : sert au reçu de paiement d'une dette."""
        r = self.conn.execute("""SELECT r.*, v.facture_no, v.total AS total_facture, c.nom AS client
                                 FROM reglements r LEFT JOIN ventes v ON v.id=r.vente_id LEFT JOIN clients c ON c.id=v.client_id WHERE r.id=?""", (rid,)).fetchone()
        if not r:
            return None
        paye = self._paye_vente(r['vente_id'])
        return dict(r, paye_total=paye, reste=max(r['total_facture'] - paye, 0))

    def get_depense(self, did):
        return self.conn.execute("SELECT * FROM depenses WHERE id=?", (did,)).fetchone()

    def get_achat(self, aid):
        return self.conn.execute("""SELECT a.*, f.nom AS fournisseur FROM achats a LEFT JOIN fournisseurs f ON f.id=a.fournisseur_id WHERE a.id=?""", (aid,)).fetchone()

    def get_achat_lignes(self, aid):
        return self.conn.execute("""SELECT l.*, ar.designation, ar.code FROM achat_lignes l LEFT JOIN articles ar ON ar.id=l.article_id
                                    WHERE l.achat_id=? ORDER BY l.id""", (aid,)).fetchall()

    def get_mouvement(self, mid):
        return self.conn.execute("""SELECT m.*, a.designation, a.code FROM mouvements m LEFT JOIN articles a ON a.id=m.article_id WHERE m.id=?""", (mid,)).fetchone()

    def get_vente(self, vid):
        return self.conn.execute("""SELECT v.*, c.nom AS client, COALESCE((SELECT SUM(montant) FROM reglements r WHERE r.vente_id=v.id AND r.annule=0),0) AS paye
                                    FROM ventes v LEFT JOIN clients c ON c.id=v.client_id WHERE v.id=?""", (vid,)).fetchone()

    def get_vente_lignes(self, vid):
        return self.conn.execute("SELECT * FROM vente_lignes WHERE vente_id=? ORDER BY id", (vid,)).fetchall()

    def get_reglements(self, vid):
        return self.conn.execute("SELECT * FROM reglements WHERE vente_id=? ORDER BY id", (vid,)).fetchall()

    def get_ventes(self, d1=None, d2=None, vendeur=None, client_id=None, term="", impayees=False, limit=500):
        q = """SELECT v.*, c.nom AS client, COALESCE((SELECT SUM(montant) FROM reglements r WHERE r.vente_id=v.id AND r.annule=0),0) AS paye
               FROM ventes v LEFT JOIN clients c ON c.id=v.client_id WHERE 1=1"""
        p = []
        if d1:
            q += " AND substr(v.date,1,10)>=?"; p.append(d1)
        if d2:
            q += " AND substr(v.date,1,10)<=?"; p.append(d2)
        if vendeur:
            q += " AND v.vendeur=?"; p.append(vendeur)
        if client_id:
            q += " AND v.client_id=?"; p.append(client_id)
        if term:
            q += " AND (v.facture_no LIKE ? OR norm(c.nom) LIKE ?)"; p += [f"%{term}%", f"%{norm(term)}%"]
        if impayees:
            q += " AND v.annule=0 AND v.total - COALESCE((SELECT SUM(montant) FROM reglements r WHERE r.vente_id=v.id AND r.annule=0),0) > 0.001"
        return self.conn.execute(q + " ORDER BY v.id DESC LIMIT ?", p + [limit]).fetchall()

    def annuler_vente(self, vid, motif):
        """Annule une vente entière : remet le stock, annule les règlements. Gérant/admin, motif obligatoire."""
        self._need('admin', 'gerant')
        v = self.get_vente(vid)
        if not v or v['annule']:
            return False, "Vente introuvable ou déjà annulée."
        if len((motif or '').strip()) < 5:
            return False, "Un motif d'au moins 5 caractères est obligatoire."
        if self.conn.execute("SELECT 1 FROM retours WHERE vente_id=?", (vid,)).fetchone():
            return False, "Cette vente a déjà des retours : annulation impossible."
        if v['date'][:10] != date.today().isoformat() and self.actor_role != 'admin':
            return False, "Seul l'administrateur peut annuler une vente d'un jour précédent."
        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        for l in self.get_vente_lignes(vid):
            self._mouvement(l['article_id'], 'annulation', l['quantite'], v['facture_no'], motif.strip())
        self.conn.execute("UPDATE reglements SET annule=1, annule_motif=? WHERE vente_id=? AND annule=0", ("Vente annulée : " + motif.strip(), vid))
        self.conn.execute("UPDATE ventes SET annule=1, annule_par=?, annule_date=?, annule_motif=? WHERE id=?", (self.actor, now, motif.strip(), vid))
        self.conn.commit()
        self.log("Vente annulée", f"{v['facture_no']} - {fcfa(v['total'])} - {motif.strip()}")
        return True, "Vente annulée, stock remis."

    def retourner(self, ligne_id, quantite, motif, remise_en_stock=True):
        """Retour client partiel. Le remboursement est calculé au prix réellement payé (remise comprise)."""
        self._need('admin', 'gerant')
        l = self.conn.execute("SELECT * FROM vente_lignes WHERE id=?", (ligne_id,)).fetchone()
        if not l:
            return False, "Ligne introuvable.", 0
        v = self.get_vente(l['vente_id'])
        if v['annule']:
            return False, "Vente annulée.", 0
        dispo = l['quantite'] - l['qte_retournee']
        if quantite <= 0 or quantite > dispo + 0.0001:
            return False, f"Quantité invalide (retournable : {dispo:g}).", 0
        if len((motif or '').strip()) < 4:
            return False, "Motif obligatoire.", 0
        montant = round(l['total'] * quantite / l['quantite'] * (1 - (v['remise'] / v['sous_total'] if v['sous_total'] else 0)))
        if remise_en_stock:
            self._mouvement(l['article_id'], 'retour', quantite, v['facture_no'], motif.strip())
        self.conn.execute("UPDATE vente_lignes SET qte_retournee=qte_retournee+? WHERE id=?", (quantite, ligne_id))
        self.conn.execute("INSERT INTO retours (date, vente_id, ligne_id, quantite, montant, motif, remise_en_stock, par) VALUES (?,?,?,?,?,?,?,?)",
                          (datetime.now().strftime('%Y-%m-%d %H:%M:%S'), l['vente_id'], ligne_id, quantite, montant, motif.strip(), int(remise_en_stock), self.actor))
        self.conn.commit()
        self.log("Retour client", f"{v['facture_no']} - {l['designation']} x{quantite:g} - remboursement {fcfa(montant)} - {motif.strip()}")
        return True, f"Retour enregistré. À rembourser : {fcfa(montant)}.", montant

    # ------------------------------------------------------------ achats & fournisseurs
    def acheter(self, fournisseur_id, lignes, paye=0, ref_fournisseur="", mode="Espèces"):
        """lignes : [(article_id, quantite, prix_achat)] -> entre en stock, met à jour le prix d'achat (dernier prix)."""
        self._need('admin', 'gerant', 'magasinier')
        if not lignes:
            return False, "Aucune ligne.", None
        try:
            total = 0
            for aid, q, pa in lignes:
                if q <= 0 or pa < 0 or not self.get_article(aid):
                    raise ValueError("Ligne d'achat invalide.")
                total += round(q * pa)
            if paye < 0 or paye > total + 0.001:
                raise ValueError("Montant payé invalide.")
            bon = self._numero('achats', 'bon_no', 'ACH')
            now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            cur = self.conn.execute("INSERT INTO achats (bon_no, date, fournisseur_id, reference_fournisseur, total, paye, par) VALUES (?,?,?,?,?,?,?)",
                                    (bon, now, fournisseur_id, ref_fournisseur, total, paye, self.actor))
            aid_achat = cur.lastrowid
            for aid, q, pa in lignes:
                self.conn.execute("INSERT INTO achat_lignes (achat_id, article_id, quantite, prix_achat, total) VALUES (?,?,?,?,?)",
                                  (aid_achat, aid, q, pa, round(q * pa)))
                self._mouvement(aid, 'achat', q, bon, ref_fournisseur)
                self.conn.execute("UPDATE articles SET prix_achat=? WHERE id=?", (pa, aid))
            if paye:
                self.conn.execute("INSERT INTO paiements_fournisseurs (date, achat_id, montant, mode, par) VALUES (?,?,?,?,?)",
                                  (now, aid_achat, paye, mode, self.actor))
        except (ValueError, sqlite3.Error) as e:
            self.conn.rollback()
            return False, str(e), None
        self.conn.commit()
        self.log("Achat / réception", f"{bon} - {len(lignes)} ligne(s) - {fcfa(total)} - payé {fcfa(paye)}")
        return True, "Achat enregistré, stock mis à jour.", aid_achat

    def payer_fournisseur(self, achat_id, montant, mode="Espèces"):
        self._need('admin', 'gerant')
        a = self.conn.execute("SELECT * FROM achats WHERE id=? AND annule=0", (achat_id,)).fetchone()
        if not a or montant <= 0 or montant > a['total'] - a['paye'] + 0.001:
            return False, "Montant invalide."
        self.conn.execute("UPDATE achats SET paye=paye+? WHERE id=?", (montant, achat_id))
        self.conn.execute("INSERT INTO paiements_fournisseurs (date, achat_id, montant, mode, par) VALUES (?,?,?,?,?)",
                          (datetime.now().strftime('%Y-%m-%d %H:%M:%S'), achat_id, montant, mode, self.actor))
        self.conn.commit()
        self.log("Paiement fournisseur", f"{a['bon_no']} - {fcfa(montant)}")
        return True, "Paiement enregistré."

    def get_achats(self, fournisseur_id=None, impayes=False):
        q = "SELECT a.*, f.nom AS fournisseur FROM achats a LEFT JOIN fournisseurs f ON f.id=a.fournisseur_id WHERE a.annule=0"
        p = []
        if fournisseur_id:
            q += " AND a.fournisseur_id=?"; p.append(fournisseur_id)
        if impayes:
            q += " AND a.total-a.paye>0.001"
        return self.conn.execute(q + " ORDER BY a.id DESC", p).fetchall()

    def suggestion_reapprovisionnement(self):
        """Articles à commander : stock <= seuil, avec quantité suggérée (2 x seuil - stock) et coût estimé."""
        out = []
        for a in self.alertes_stock():
            q = max(1, round(a['seuil_alerte'] * 2 - a['stock']))
            out.append(dict(id=a['id'], code=a['code'], designation=a['designation'], fournisseur=a['fournisseur'],
                            stock=a['stock'], seuil=a['seuil_alerte'], a_commander=q, cout=q * a['prix_achat']))
        return out

    # ------------------------------------------------------------ dépenses
    def add_depense(self, libelle, montant, categorie=""):
        self._need('admin', 'gerant', 'vendeur')
        if montant <= 0 or not libelle.strip():
            return False, "Libellé et montant obligatoires."
        if self.cloture_active(date.today().isoformat(), self.actor):
            return False, "Cette caisse est clôturée pour aujourd'hui."
        cur = self.conn.execute("INSERT INTO depenses (date, libelle, montant, categorie, par) VALUES (?,?,?,?,?)",
                                (datetime.now().strftime('%Y-%m-%d %H:%M:%S'), libelle.strip(), montant, categorie, self.actor))
        self.last_depense_id = cur.lastrowid
        self.conn.commit()
        self.log("Dépense", f"{libelle.strip()} - {fcfa(montant)}")
        return True, "Dépense enregistrée."

    # ------------------------------------------------------------ caisse & clôture
    def caisse_du_jour(self, jour, caissier):
        regs = self.conn.execute("""SELECT r.*, v.facture_no FROM reglements r LEFT JOIN ventes v ON v.id=r.vente_id
                                    WHERE substr(r.date,1,10)=? AND r.caissier=? AND r.annule=0""", (jour, caissier)).fetchall()
        dep = self.conn.execute("SELECT COALESCE(SUM(montant),0) FROM depenses WHERE substr(date,1,10)=? AND par=?", (jour, caissier)).fetchone()[0]
        rem = self.conn.execute("""SELECT COALESCE(SUM(montant),0) FROM retours WHERE substr(date,1,10)=? AND par=?""", (jour, caissier)).fetchone()[0]
        par_mode = {}
        for r in regs:
            d = par_mode.setdefault(r['mode'], [0, 0.0]); d[0] += 1; d[1] += r['montant']
        especes = sum(r['montant'] for r in regs if r['mode'] == 'Espèces')
        total = sum(r['montant'] for r in regs)
        return dict(nb=len(regs), total=total, especes=especes, autres=total - especes, depenses=dep, remboursements=rem,
                    attendu=especes - dep - rem, par_mode=par_mode, cloture=self.cloture_active(jour, caissier))

    def cloture_active(self, jour, caissier):
        return self.conn.execute("SELECT * FROM clotures WHERE jour=? AND caissier=? AND rouverte=0", (jour, caissier)).fetchone()

    def cloturer(self, jour, caissier, compte, note=""):
        if self.actor_role != 'admin' and caissier != self.actor:
            return False, "Vous ne pouvez clôturer que votre propre caisse.", None
        if jour > date.today().isoformat():
            return False, "Impossible de clôturer une journée à venir.", None
        if self.cloture_active(jour, caissier):
            return False, "Cette caisse est déjà clôturée.", None
        d = self.caisse_du_jour(jour, caissier)
        ecart = compte - d['attendu']
        if abs(ecart) > 0.001 and len((note or '').strip()) < 5:
            return False, f"Écart de caisse de {fcfa(ecart)} : une explication d'au moins 5 caractères est obligatoire.", None
        cur = self.conn.execute("""INSERT INTO clotures (jour, caissier, nb, total_especes, total_autres, depenses, attendu, compte, ecart, note, cree_par, cree_le)
                                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                                (jour, caissier, d['nb'], d['especes'], d['autres'], d['depenses'] + d['remboursements'], d['attendu'], compte, ecart,
                                 (note or '').strip() or None, self.actor, datetime.now().strftime('%Y-%m-%d %H:%M:%S')))
        self.conn.commit()
        self.log("Clôture de caisse", f"{to_fr(jour)} - {caissier} - attendu {fcfa(d['attendu'])}, compté {fcfa(compte)}, écart {fcfa(ecart)}")
        return True, "Caisse clôturée.", cur.lastrowid

    def rouvrir_cloture(self, cid, motif):
        self._need('admin', 'gerant')
        if len((motif or '').strip()) < 5:
            return False, "Motif obligatoire (5 caractères minimum)."
        c = self.conn.execute("SELECT * FROM clotures WHERE id=? AND rouverte=0", (cid,)).fetchone()
        if not c:
            return False, "Clôture introuvable."
        self.conn.execute("UPDATE clotures SET rouverte=1, rouverte_par=?, rouverte_motif=? WHERE id=?", (self.actor, motif.strip(), cid))
        self.conn.commit()
        self.log("Clôture rouverte", f"{to_fr(c['jour'])} - {c['caissier']} - {motif.strip()}")
        return True, "Caisse rouverte."

    # ------------------------------------------------------------ rapports
    @staticmethod
    def _periode_valide(d1, d2):
        try:
            a, b = datetime.strptime(str(d1), '%Y-%m-%d'), datetime.strptime(str(d2), '%Y-%m-%d')
        except ValueError:
            raise ValueError("Dates invalides : utilisez le format AAAA-MM-JJ (ex. 2026-11-30).")
        if a > b:
            raise ValueError("La date de début doit précéder la date de fin.")

    def inventaire_periode(self, d1, d2, tout=False):
        """Inventaire de la boutique pour une période : par article, stock au début, achats, retours, ventes, pertes/ajustements, stock à la fin,
        et valeur du stock. Reconstitué à partir de l'historique complet des mouvements (rien n'est saisi à la main) :
        stock fin = stock actuel moins les mouvements postérieurs à la période ; stock début = stock fin moins les mouvements de la période.
        Valorisation au prix d'achat / de vente actuel de chaque article. tout=False : seuls les articles avec du stock ou des mouvements."""
        self._need('admin', 'gerant')
        self._periode_valide(d1, d2)
        rows = self.conn.execute("""
            SELECT a.id, a.code, a.designation, c.nom AS categorie, a.stock, a.prix_achat, a.prix_vente, a.actif,
              COALESCE(SUM(m.quantite), 0) AS net,
              COALESCE(SUM(CASE WHEN substr(m.date,1,10) > :d2 THEN m.quantite END), 0) AS apres,
              COALESCE(SUM(CASE WHEN substr(m.date,1,10) BETWEEN :d1 AND :d2 AND m.type='achat' THEN m.quantite END), 0) AS achats,
              COALESCE(SUM(CASE WHEN substr(m.date,1,10) BETWEEN :d1 AND :d2 AND m.type IN ('retour','annulation') THEN m.quantite END), 0) AS retours,
              COALESCE(SUM(CASE WHEN substr(m.date,1,10) BETWEEN :d1 AND :d2 AND m.type='vente' THEN -m.quantite END), 0) AS ventes,
              COALESCE(SUM(CASE WHEN substr(m.date,1,10) BETWEEN :d1 AND :d2 AND m.type IN ('perte','ajustement','inventaire') THEN m.quantite END), 0) AS ajust
            FROM articles a JOIN categories c ON c.id=a.categorie_id LEFT JOIN mouvements m ON m.article_id=a.id
            GROUP BY a.id ORDER BY c.nom, a.designation""", {'d1': d1, 'd2': d2}).fetchall()
        lignes, cats = [], {}
        for r in rows:
            fin = r['stock'] - r['apres']
            debut = fin - (r['achats'] + r['retours'] - r['ventes'] + r['ajust'])
            if not tout and abs(fin) < 1e-9 and abs(debut) < 1e-9 and not (r['achats'] or r['retours'] or r['ventes'] or r['ajust']):
                continue
            pa, pv = r['prix_achat'] or 0, r['prix_vente'] or 0
            l = dict(id=r['id'], code=r['code'], designation=r['designation'], categorie=r['categorie'], debut=debut, achats=r['achats'],
                     retours=r['retours'], ventes=r['ventes'], ajust=r['ajust'], fin=fin, prix_achat=pa, prix_vente=pv,
                     valeur_debut=debut * pa, valeur_achat=fin * pa, valeur_vente=fin * pv, actif=r['actif'],
                     ecart=round(r['stock'] - r['net'], 6))       # ≠ 0 : stock modifié hors de l'application (historique incomplet)
            lignes.append(l)
            c = cats.setdefault(r['categorie'], dict(categorie=r['categorie'], nb=0, debut=0, fin=0, valeur_achat=0, valeur_vente=0))
            c['nb'] += 1; c['debut'] += debut; c['fin'] += fin; c['valeur_achat'] += l['valeur_achat']; c['valeur_vente'] += l['valeur_vente']
        tot = dict(nb=len(lignes), debut=sum(l['debut'] for l in lignes), achats=sum(l['achats'] for l in lignes), retours=sum(l['retours'] for l in lignes),
                   ventes=sum(l['ventes'] for l in lignes), ajust=sum(l['ajust'] for l in lignes), fin=sum(l['fin'] for l in lignes),
                   valeur_debut=sum(l['valeur_debut'] for l in lignes), valeur_achat=sum(l['valeur_achat'] for l in lignes),
                   valeur_vente=sum(l['valeur_vente'] for l in lignes))
        tot['marge_potentielle'] = tot['valeur_vente'] - tot['valeur_achat']
        anomalies = [dict(code=l['code'], designation=l['designation'], ecart=l['ecart']) for l in lignes if abs(l['ecart']) > 1e-9]
        tot['anomalies'] = len(anomalies)
        return dict(anomalies=anomalies, d1=d1, d2=d2, tout=bool(tout), genere=datetime.now().strftime('%Y-%m-%d %H:%M:%S'), par=self.actor,
                    lignes=lignes, categories=list(cats.values()), totaux=tot)

    def journaliser_inventaire(self, d1, d2, tout=False):
        """Inscrit au journal d'audit qu'un inventaire a été édité (période, effectif, valeurs) : les chiffres sont recalculés ici,
        jamais fournis par le poste qui demande l'inscription."""
        self._need('admin', 'gerant')
        t = self.inventaire_periode(d1, d2, tout)['totaux']
        self.log("Inventaire de la boutique", f"Période du {d1} au {d2} - {t['nb']} article(s) - stock fin {t['fin']:g} unité(s) - "
                 f"valeur à l'achat {t['valeur_achat']:,.0f} FCFA - valeur à la vente {t['valeur_vente']:,.0f} FCFA".replace(',', ' ')
                 + (f" - {t['anomalies']} article(s) au stock non justifié par l'historique" if t['anomalies'] else ""))
        self.conn.commit()
        return t

    def journaliser_audit(self, d1, d2):
        """Trace l'impression du journal d'audit lui-même."""
        self._need('admin', 'gerant')
        self._periode_valide(d1, d2)
        n = self.conn.execute("SELECT COUNT(*) FROM audit WHERE substr(date,1,10) BETWEEN ? AND ?", (d1, d2)).fetchone()[0]
        self.log("Journal d'audit imprimé", f"Période du {d1} au {d2} - {n} entrée(s)")
        self.conn.commit()
        return n

    def resume_periode(self, d1, d2):
        """Chiffre d'affaires, marge brute, encaissé, dépenses, bénéfice net estimé sur une période."""
        v = self.conn.execute("""SELECT COUNT(*) n, COALESCE(SUM(total),0) ca, COALESCE(SUM(remise),0) rem FROM ventes
                                 WHERE annule=0 AND substr(date,1,10) BETWEEN ? AND ?""", (d1, d2)).fetchone()
        cout = self.conn.execute("""SELECT COALESCE(SUM((l.quantite-l.qte_retournee)*l.prix_achat),0) FROM vente_lignes l JOIN ventes v ON v.id=l.vente_id
                                    WHERE v.annule=0 AND substr(v.date,1,10) BETWEEN ? AND ?""", (d1, d2)).fetchone()[0]
        ret = self.conn.execute("SELECT COALESCE(SUM(montant),0) FROM retours WHERE substr(date,1,10) BETWEEN ? AND ?", (d1, d2)).fetchone()[0]
        enc = self.conn.execute("SELECT COALESCE(SUM(montant),0) FROM reglements WHERE annule=0 AND substr(date,1,10) BETWEEN ? AND ?", (d1, d2)).fetchone()[0]
        dep = self.conn.execute("SELECT COALESCE(SUM(montant),0) FROM depenses WHERE substr(date,1,10) BETWEEN ? AND ?", (d1, d2)).fetchone()[0]
        ca_net = v['ca'] - ret
        marge = ca_net - cout
        creances = self.conn.execute("""SELECT COALESCE(SUM(v.total - COALESCE((SELECT SUM(montant) FROM reglements r WHERE r.vente_id=v.id AND r.annule=0),0)),0)
                                        FROM ventes v WHERE v.annule=0""").fetchone()[0]
        dettes = self.conn.execute("SELECT COALESCE(SUM(total-paye),0) FROM achats WHERE annule=0").fetchone()[0]
        return dict(nb_ventes=v['n'], ca=ca_net, remises=v['rem'], retours=ret, cout=cout, marge=marge,
                    taux_marge=(marge / ca_net * 100 if ca_net else 0), encaisse=enc, depenses=dep, benefice=marge - dep,
                    creances=creances, dettes_fournisseurs=dettes)

    def ventes_par_famille(self, d1, d2):
        return self.conn.execute("""SELECT c.famille, c.nom AS categorie, SUM(l.quantite-l.qte_retournee) qte,
                                    SUM(l.total) ca, SUM(l.total - (l.quantite*l.prix_achat)) marge
                                    FROM vente_lignes l JOIN ventes v ON v.id=l.vente_id JOIN articles a ON a.id=l.article_id
                                    JOIN categories c ON c.id=a.categorie_id
                                    WHERE v.annule=0 AND substr(v.date,1,10) BETWEEN ? AND ? GROUP BY c.id ORDER BY ca DESC""", (d1, d2)).fetchall()

    def top_articles(self, d1, d2, limit=20):
        return self.conn.execute("""SELECT a.code, a.designation, SUM(l.quantite-l.qte_retournee) qte, SUM(l.total) ca
                                    FROM vente_lignes l JOIN ventes v ON v.id=l.vente_id JOIN articles a ON a.id=l.article_id
                                    WHERE v.annule=0 AND substr(v.date,1,10) BETWEEN ? AND ? GROUP BY a.id
                                    ORDER BY qte DESC LIMIT ?""", (d1, d2, limit)).fetchall()

    def articles_dormants(self, jours=90):
        """Articles en stock sans aucune vente depuis N jours (capital immobilisé)."""
        lim = (date.today() - timedelta(days=jours)).isoformat()
        return self.conn.execute("""SELECT a.code, a.designation, a.stock, a.stock*a.prix_achat AS immobilise,
                                    (SELECT MAX(substr(v.date,1,10)) FROM vente_lignes l JOIN ventes v ON v.id=l.vente_id
                                     WHERE l.article_id=a.id AND v.annule=0) AS derniere_vente
                                    FROM articles a WHERE a.actif=1 AND a.stock>0
                                    AND NOT EXISTS (SELECT 1 FROM vente_lignes l JOIN ventes v ON v.id=l.vente_id
                                                    WHERE l.article_id=a.id AND v.annule=0 AND substr(v.date,1,10)>=?)
                                    ORDER BY immobilise DESC""", (lim,)).fetchall()

    def ventes_par_jour(self, d1, d2):
        return self.conn.execute("""SELECT substr(date,1,10) jour, COUNT(*) nb, SUM(total) ca FROM ventes
                                    WHERE annule=0 AND substr(date,1,10) BETWEEN ? AND ? GROUP BY jour ORDER BY jour""", (d1, d2)).fetchall()

    def ventes_par_vendeur(self, d1, d2):
        return self.conn.execute("""SELECT vendeur, COUNT(*) nb, SUM(total) ca FROM ventes
                                    WHERE annule=0 AND substr(date,1,10) BETWEEN ? AND ? GROUP BY vendeur ORDER BY ca DESC""", (d1, d2)).fetchall()

    def top_clients(self, d1, d2, limit=10):
        return self.conn.execute("""SELECT c.nom, COUNT(*) nb, SUM(v.total) ca FROM ventes v JOIN clients c ON c.id=v.client_id
                                    WHERE v.annule=0 AND substr(v.date,1,10) BETWEEN ? AND ? GROUP BY c.id ORDER BY ca DESC LIMIT ?""", (d1, d2, limit)).fetchall()

    # ------------------------------------------------------------ packs scolaires (liste de fournitures d'une classe)
    def get_packs(self):
        return self.conn.execute("""SELECT p.*, (SELECT COUNT(*) FROM pack_lignes l WHERE l.pack_id=p.id) AS nb_articles,
                                    (SELECT COALESCE(SUM(ROUND(l.quantite*a.prix_vente)),0) FROM pack_lignes l JOIN articles a ON a.id=l.article_id
                                     WHERE l.pack_id=p.id) AS total FROM packs p ORDER BY p.niveau, p.nom""").fetchall()

    def get_pack_lignes(self, pid):
        """Lignes d'un pack avec le prix actuel et le stock disponible."""
        return self.conn.execute("""SELECT l.article_id, l.quantite, a.code, a.designation, a.prix_vente, a.stock, a.actif
                                    FROM pack_lignes l JOIN articles a ON a.id=l.article_id WHERE l.pack_id=? ORDER BY a.designation""", (pid,)).fetchall()

    def save_pack(self, pid, nom, niveau, lignes):
        """lignes : [(article_id, quantite)]. Remplace le contenu du pack."""
        self._need('admin', 'gerant', 'magasinier')
        nom = (nom or '').strip()
        if not nom:
            return False, "Nom du pack obligatoire."
        merged = {}
        for aid, q in lignes:
            if q <= 0 or not self.get_article(aid):
                return False, "Ligne de pack invalide."
            merged[aid] = merged.get(aid, 0) + q
        if not merged:
            return False, "Un pack doit contenir au moins un article."
        try:
            if pid:
                self.conn.execute("UPDATE packs SET nom=?, niveau=? WHERE id=?", (nom, niveau, pid))
                self.conn.execute("DELETE FROM pack_lignes WHERE pack_id=?", (pid,))
            else:
                pid = self.conn.execute("INSERT INTO packs (nom, niveau) VALUES (?,?)", (nom, niveau)).lastrowid
            self.conn.executemany("INSERT INTO pack_lignes (pack_id, article_id, quantite) VALUES (?,?,?)", [(pid, a, q) for a, q in merged.items()])
        except sqlite3.IntegrityError:
            self.conn.rollback()
            return False, "Un pack porte déjà ce nom."
        self.conn.commit()
        self.log("Pack enregistré", f"{nom} ({len(merged)} article(s))")
        return True, "Pack enregistré."

    def delete_pack(self, pid):
        self._need('admin', 'gerant')
        r = self.conn.execute("SELECT nom FROM packs WHERE id=?", (pid,)).fetchone()
        if r:
            self.conn.execute("DELETE FROM pack_lignes WHERE pack_id=?", (pid,))
            self.conn.execute("DELETE FROM packs WHERE id=?", (pid,))
            self.conn.commit()
            self.log("Pack supprimé", r['nom'])
        return True

    # ------------------------------------------------------------ import catalogue (CSV / Excel)
    def import_articles(self, rows):
        """rows : liste de dicts avec au minimum designation, categorie, prix_vente. Catégorie créée si absente."""
        self._need('admin', 'gerant', 'magasinier')
        ok, erreurs = 0, []
        cats = {norm(c['nom']): c['id'] for c in self.get_categories()}
        for i, r in enumerate(rows, 2):
            try:
                cat = norm(r.get('categorie', ''))
                if cat not in cats:
                    self.conn.execute("INSERT INTO categories (nom, famille) VALUES (?,?)", (str(r.get('categorie', 'Divers')).strip() or 'Divers', 'autre'))
                    cats[cat] = self.conn.execute("SELECT last_insert_rowid()").fetchone()[0]
                pv = float(str(r.get('prix_vente', '')).replace(' ', '').replace(',', '.') or 0)
                pa = float(str(r.get('prix_achat', '') or 0).replace(' ', '').replace(',', '.') or 0)
                st = float(str(r.get('stock', '') or 0).replace(' ', '').replace(',', '.') or 0)
                good, msg = self.save_article(None, str(r.get('designation', '')), cats[cat], pv, pa, str(r.get('code', '') or ''),
                                              str(r.get('auteur', '') or ''), str(r.get('editeur', '') or ''), str(r.get('isbn', '') or ''),
                                              str(r.get('niveau', '') or ''), stock_initial=st)
                ok += good
                if not good:
                    erreurs.append(f"Ligne {i} : {msg}")
            except (ValueError, KeyError) as e:
                erreurs.append(f"Ligne {i} : {e}")
        self.conn.commit()
        self.log("Import catalogue", f"{ok} article(s) importé(s), {len(erreurs)} erreur(s)")
        return ok, erreurs

    # ------------------------------------------------------------ sauvegarde
    def backup(self, dest=None, keep=30, externe=True):
        """Sauvegarde atomique : copie dans un fichier temporaire, vérifie son intégrité, puis renomme.
        Si un dossier externe (clé USB, cloud synchronisé) est configuré, une copie y est déposée aussi."""
        os.makedirs(BACKUP_DIR, exist_ok=True)
        dest = dest or os.path.join(BACKUP_DIR, f"boutique_{datetime.now().strftime('%Y%m%d_%H%M%S')}.db")
        tmp = dest + '.part'
        self.conn.commit()
        try:
            out = sqlite3.connect(tmp)
            try:
                self.conn.backup(out)
                if out.execute("PRAGMA integrity_check").fetchone()[0] != 'ok':
                    raise sqlite3.DatabaseError("La copie de sauvegarde est corrompue.")
            finally:
                out.close()
            os.replace(tmp, dest)
        except (sqlite3.Error, OSError):
            try:
                os.remove(tmp)
            except OSError:
                pass
            raise
        self._purger(BACKUP_DIR, keep)
        self.log("Sauvegarde", os.path.basename(dest))
        if externe:
            self.copie_externe(dest, keep)
        return dest

    @staticmethod
    def _purger(dossier, keep):
        olds = sorted(f for f in os.listdir(dossier) if f.startswith('boutique_') and f.endswith('.db'))
        for f in olds[:-keep]:
            try:
                os.remove(os.path.join(dossier, f))
            except OSError:
                pass

    def copie_externe(self, source, keep=30):
        """Dépose une copie de la sauvegarde dans le dossier externe. Ne lève jamais d'erreur : le résultat est dans
        self.ext_etat = (ok, message), car une clé USB débranchée ne doit pas bloquer la boutique."""
        ext = (get_config().get('backup_externe') or '').strip()
        if not ext:
            self.ext_etat = (None, "Aucune copie externe configurée.")
            return False
        if not os.path.isdir(ext):
            self.ext_etat = (False, f"Dossier externe introuvable : {ext} (clé USB débranchée ?)")
            return False
        try:
            cible = os.path.join(ext, os.path.basename(source))
            shutil.copy2(source, cible + '.part')
            os.replace(cible + '.part', cible)
            self._purger(ext, keep)
        except OSError as e:
            self.ext_etat = (False, f"Copie externe impossible : {e}")
            return False
        self.ext_etat = (True, f"Copie externe déposée dans {ext}")
        return True

    def etat_sauvegarde(self):
        """Pour l'écran d'accueil : âge de la dernière sauvegarde locale et état de la copie externe."""
        fichiers = self.liste_sauvegardes()
        locale = next((f for f in fichiers if f['lieu'] == 'locale'), None)
        ext = (get_config().get('backup_externe') or '').strip()
        ext_ok = bool(ext) and os.path.isdir(ext)
        derniere_ext = next((f for f in fichiers if f['lieu'] == 'externe'), None)
        return dict(derniere=locale['date'] if locale else None, externe_configuree=bool(ext), externe_disponible=ext_ok,
                    derniere_externe=derniere_ext['date'] if derniere_ext else None, dossier_externe=ext)

    def liste_sauvegardes(self):
        """Sauvegardes locales et externes, de la plus récente à la plus ancienne."""
        res = []
        ext = (get_config().get('backup_externe') or '').strip()
        for lieu, dossier in (('locale', BACKUP_DIR), ('externe', ext)):
            if not dossier or not os.path.isdir(dossier):
                continue
            try:
                for f in os.listdir(dossier):
                    if f.endswith('.db') and (f.startswith('boutique_') or f.startswith('avant_restauration_')):
                        chemin = os.path.join(dossier, f)
                        st = os.stat(chemin)
                        res.append(dict(chemin=chemin, nom=f, lieu=lieu, taille=st.st_size,
                                        date=datetime.fromtimestamp(st.st_mtime).strftime('%Y-%m-%d %H:%M:%S')))
            except OSError:
                continue
        return sorted(res, key=lambda x: x['date'], reverse=True)

    @staticmethod
    def verifier_sauvegarde(chemin):
        """Contrôle un fichier avant restauration. Retourne (ok, message)."""
        if not os.path.isfile(chemin):
            return False, "Fichier introuvable."
        try:
            c = sqlite3.connect(f"file:{chemin}?mode=ro", uri=True)
            try:
                if c.execute("PRAGMA integrity_check").fetchone()[0] != 'ok':
                    return False, "Ce fichier est endommagé."
                tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if not {'articles', 'ventes', 'users', 'audit'} <= tables:
                    return False, "Ce fichier n'est pas une sauvegarde de cette application."
                n = c.execute("SELECT COUNT(*) FROM ventes").fetchone()[0]
            finally:
                c.close()
        except sqlite3.Error as e:
            return False, f"Fichier illisible : {e}"
        return True, f"Sauvegarde valide ({n} vente(s))."

    def restaurer(self, chemin):
        """Remplace le contenu de la base par celui d'une sauvegarde (administrateur seulement, poste serveur).
        Une copie de sécurité de l'état actuel est faite d'abord, pour pouvoir revenir en arrière."""
        self._need('admin')
        ok, msg = self.verifier_sauvegarde(chemin)
        if not ok:
            return False, msg
        os.makedirs(BACKUP_DIR, exist_ok=True)
        secours = os.path.join(BACKUP_DIR, f"avant_restauration_{datetime.now().strftime('%Y%m%d_%H%M%S')}.db")
        self.backup(dest=secours, externe=False)
        self.conn.commit()
        src = sqlite3.connect(f"file:{chemin}?mode=ro", uri=True)
        try:
            src.backup(self.conn)
        except sqlite3.Error as e:
            return False, f"Restauration interrompue : {e}. Votre base précédente est conservée dans {os.path.basename(secours)}."
        finally:
            src.close()
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.create_tables()
        self.conn.commit()
        self.log("Restauration", f"{os.path.basename(chemin)} (état précédent : {os.path.basename(secours)})")
        return True, f"Restauration effectuée. L'état précédent est conservé : {os.path.basename(secours)}."

    # ------------------------------------------------------------ écran d'accueil
    def tableau_de_bord(self):
        """Toutes les données de l'écran d'accueil en un seul appel (efficace en réseau). Adapté au rôle."""
        jour = date.today().isoformat()
        role = self.actor_role
        out = dict(jour=jour, role=role)
        r = self.conn.execute("""SELECT COALESCE(SUM(stock<=0),0) rupt, COALESCE(SUM(stock>0 AND stock<=seuil_alerte),0) bas, COUNT(*) n
                                 FROM articles WHERE actif=1""").fetchone()
        out.update(ruptures=r['rupt'], stock_bas=r['bas'], nb_articles=r['n'])
        if role in ('admin', 'gerant', 'vendeur'):
            q = "SELECT COUNT(*) n, COALESCE(SUM(total),0) ca FROM ventes WHERE annule=0 AND substr(date,1,10)=?"
            p = [jour]
            if role == 'vendeur':
                q += " AND vendeur=?"; p.append(self.actor)
            v = self.conn.execute(q, p).fetchone()
            out.update(ventes_jour=v['n'], ca_jour=v['ca'])
            qc = "SELECT COALESCE(SUM(montant),0) FROM reglements WHERE annule=0 AND mode='Espèces' AND substr(date,1,10)=?"
            pc = [jour]
            if role == 'vendeur':
                qc += " AND caissier=?"; pc.append(self.actor)
            out['especes_jour'] = self.conn.execute(qc, pc).fetchone()[0]
            out['caisse_cloturee'] = bool(self.cloture_active(jour, self.actor))
            out['impayees'] = self.conn.execute("""SELECT COUNT(*) FROM ventes v WHERE v.annule=0 AND v.total - COALESCE((SELECT SUM(montant)
                                FROM reglements r WHERE r.vente_id=v.id AND r.annule=0),0) > 0.001""").fetchone()[0]
        if role in ('admin', 'gerant'):
            debut_mois = jour[:8] + '01'
            j = self.resume_periode(jour, jour)
            m = self.resume_periode(debut_mois, jour)
            out.update(marge_jour=j['marge'], benefice_jour=j['benefice'], depenses_jour=j['depenses'],
                       ca_mois=m['ca'], marge_mois=m['marge'], taux_marge_mois=m['taux_marge'], nb_ventes_mois=m['nb_ventes'],
                       creances=j['creances'], dettes_fournisseurs=j['dettes_fournisseurs'])
            d0 = (date.today() - timedelta(days=6)).isoformat()
            par_jour = {x['jour']: x['ca'] for x in self.ventes_par_jour(d0, jour)}
            out['sept_jours'] = [((date.today() - timedelta(days=6 - i)).isoformat(),
                                  par_jour.get((date.today() - timedelta(days=6 - i)).isoformat(), 0)) for i in range(7)]
            out['top_mois'] = [(x['designation'], x['qte'], x['ca']) for x in self.top_articles(debut_mois, jour, 5)]
            out['valeur_stock'] = self.valeur_stock()['achat']
        return out

    def close(self):
        self.conn.commit()
        self.conn.close()
