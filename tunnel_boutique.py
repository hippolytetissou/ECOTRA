# -*- coding: utf-8 -*-
"""
ACCÈS PAR INTERNET (tunnel_boutique.py)

Problème résolu : le téléphone n'arrive pas à joindre l'ordinateur par Wi-Fi / point d'accès / Bluetooth (pare-feu, isolation des
appareils, téléphone qui est lui-même le point d'accès...). Ici, plus aucun réseau local n'est nécessaire : l'ordinateur ouvre
lui-même une liaison sortante et sécurisée (HTTPS) vers Internet via « Cloudflare Quick Tunnel », et le téléphone ouvre l'adresse
https://xxxx.trycloudflare.com/m qui lui est donnée, par 4G ou par n'importe quel Wi-Fi.

  - Nécessite Internet sur l'ordinateur (le point d'accès du téléphone convient).
  - Le petit programme « cloudflared » (Apache 2.0, ~30 Mo) est téléchargé UNE fois depuis le site officiel de Cloudflare.
  - L'adresse change à chaque activation. Seules les personnes qui ont l'adresse + le code d'accès + un identifiant et un mot de passe
    peuvent entrer ; seule la page du téléphone est exposée (jamais le reste du serveur).
  - Gratuit, sans compte, sans garantie de disponibilité de la part de Cloudflare.
"""
import os
import platform
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

import database_boutique as D

BASE_URL = "https://github.com/cloudflare/cloudflared/releases/latest/download/"
URL_RE = re.compile(r'https://([a-z0-9-]+)\.trycloudflare\.com')
TAILLE_MIN = 5_000_000                       # un binaire cloudflared fait plus de 5 Mo : en dessous, le téléchargement est incomplet


class ErreurTunnel(Exception):
    """Erreur expliquée en français, affichable telle quelle à l'utilisateur."""


def nom_binaire():
    m = platform.machine().lower()
    if os.name == 'nt':
        return "cloudflared-windows-" + ("386.exe" if m in ('x86', 'i386', 'i686') else "amd64.exe")
    if sys.platform.startswith('linux'):
        return "cloudflared-linux-" + ("arm64" if m in ('aarch64', 'arm64') else "amd64")
    raise ErreurTunnel("Ce système n'est pas pris en charge pour l'accès par Internet (Windows ou Linux requis).")


def chemin_binaire():
    return os.path.join(D.BASE_DIR, 'outils', 'cloudflared' + ('.exe' if os.name == 'nt' else ''))


def installe():
    p = chemin_binaire()
    return os.path.isfile(p) and os.path.getsize(p) >= TAILLE_MIN


def telecharger(progression=None, base_url=None, timeout=30):
    """Télécharge cloudflared (une seule fois). progression(lu, total) est appelée régulièrement. Lève ErreurTunnel avec un message clair."""
    dest = chemin_binaire()
    tmp = dest + '.part'
    url = (base_url or BASE_URL) + nom_binaire()
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'GestionBoutique'})
        with urllib.request.urlopen(req, timeout=timeout) as r, open(tmp, 'wb') as f:
            total, lu = int(r.headers.get('Content-Length') or 0), 0
            while True:
                bloc = r.read(65536)
                if not bloc:
                    break
                f.write(bloc)
                lu += len(bloc)
                if progression:
                    progression(lu, total)
        if os.path.getsize(tmp) < TAILLE_MIN:
            raise ErreurTunnel("Téléchargement incomplet. Vérifiez la connexion Internet puis réessayez.")
        with open(tmp, 'rb') as f:
            tete = f.read(4)
        if (os.name == 'nt' and tete[:2] != b'MZ') or (os.name != 'nt' and tete != b'\x7fELF'):
            raise ErreurTunnel("Le fichier téléchargé n'est pas valide (page de blocage d'un pare-feu ou d'un antivirus ?).")
        os.replace(tmp, dest)
        if os.name != 'nt':
            os.chmod(dest, 0o755)
    except urllib.error.HTTPError as e:
        _nettoyer(tmp)
        raise ErreurTunnel(f"Le téléchargement est refusé (code {e.code}). Réessayez plus tard.")
    except (urllib.error.URLError, OSError, TimeoutError) as e:
        _nettoyer(tmp)
        raise ErreurTunnel(f"Téléchargement impossible : Internet fonctionne-t-il sur cet ordinateur ? ({getattr(e, 'reason', e)})")
    except ErreurTunnel:
        _nettoyer(tmp)
        raise


def _nettoyer(chemin):
    try:
        os.remove(chemin)
    except OSError:
        pass


def verifier(url, timeout=15):
    """Teste l'adresse publique DEPUIS cet ordinateur (comme le ferait le téléphone).
    Retourne ('ok' | 'pas_connecte' | 'inconnu', explication). « pas_connecte » = erreur Cloudflare 1033 / 530 : le tunnel n'est pas encore relié."""
    try:
        req = urllib.request.Request(url.rstrip('/') + '/m', headers={'User-Agent': 'GestionBoutique'})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            corps = r.read(200000)
            if r.status == 200 and b'Gestion boutique' in corps:
                return 'ok', "L'adresse répond : la page de la boutique est joignable depuis Internet."
            return 'inconnu', f"Réponse inattendue (code {r.status})."
    except urllib.error.HTTPError as e:
        if e.code in (530, 502, 404):
            return 'pas_connecte', f"Cloudflare répond « {e.code} » : le tunnel n'est pas (encore) relié à cet ordinateur."
        return 'inconnu', f"Réponse inattendue (code {e.code})."
    except (urllib.error.URLError, OSError, TimeoutError) as e:
        return 'inconnu', f"Vérification impossible depuis cet ordinateur ({getattr(e, 'reason', e)}). Le téléphone pourra peut-être quand même ouvrir l'adresse."


def _diagnostic(lignes):
    """Traduit les dernières lignes de cloudflared en une cause compréhensible."""
    texte = " ".join(lignes[-12:]).lower()
    if 'no such host' in texte or 'lookup' in texte and 'srv' in texte or 'dns' in texte:
        return "Le service de noms (DNS) de ce réseau bloque la connexion à Cloudflare."
    if 'i/o timeout' in texte or 'timeout' in texte or 'dial tcp' in texte or 'connection refused' in texte or 'network is unreachable' in texte:
        return "Ce réseau bloque la connexion sortante vers Cloudflare (port 7844). Essayez un autre Wi-Fi ou un autre opérateur."
    if 'certificate' in texte or 'x509' in texte:
        return "La date et l'heure de l'ordinateur sont fausses, ou un antivirus intercepte la connexion sécurisée."
    return ""


class Tunnel:
    """Processus cloudflared + adresse publique. L'adresse n'est considérée PRÊTE que lorsque la liaison avec Cloudflare est réellement établie
    (sinon le téléphone voit l'erreur Cloudflare 1033). cmd : liste, ou fonction(protocole) -> liste (tests)."""
    def __init__(self, port, cmd=None, token='', adresse=''):
        self.port, self._cmd = port, cmd
        self.token = (token or '').strip()
        a = (adresse or '').strip().rstrip('/')
        self.url_fixe = (a if a.startswith('http') else f'https://{a}') if a else ''     # adresse FIXE (tunnel nommé) : ne change jamais
        self.proc, self.url, self.erreur, self.lignes = None, '', '', []
        self.n_reg, self.essai = 0, ''

    @property
    def connecte(self):
        return self.n_reg > 0

    def commande(self, protocole='http2'):
        if callable(self._cmd):
            return self._cmd(protocole)
        if self._cmd:
            return self._cmd
        if self.token and self.url_fixe:                                  # tunnel nommé : l'adresse est celle configurée chez Cloudflare
            return [chemin_binaire(), 'tunnel', '--no-autoupdate', '--protocol', protocole, '--edge-ip-version', '4', 'run', '--token', self.token]
        return [chemin_binaire(), 'tunnel', '--url', f'http://127.0.0.1:{self.port}', '--no-autoupdate',
                '--protocol', protocole, '--edge-ip-version', '4']

    def demarrer(self, protocole='http2'):
        if self.proc and self.proc.poll() is None:
            return
        self.url, self.erreur = self.url_fixe, ''
        self.lignes, self.n_reg = [], 0
        try:
            self.proc = subprocess.Popen(self.commande(protocole), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding='utf-8', errors='replace',
                                         creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        except OSError as e:
            raise ErreurTunnel(f"Impossible de lancer l'outil de connexion ({e}). Un antivirus l'a peut-être supprimé : réactivez l'accès par Internet.")
        threading.Thread(target=self._lire, args=(self.proc,), daemon=True).start()

    def _lire(self, proc):
        for ligne in proc.stdout:
            self.lignes.append(ligne.strip())
            del self.lignes[:-40]
            bas = ligne.lower()
            if 'unregistered tunnel connection' in bas:
                self.n_reg = max(0, self.n_reg - 1)
            elif 'registered tunnel connection' in bas:
                self.n_reg += 1
            for m in URL_RE.finditer(ligne):
                if m.group(1) != 'api' and not self.url:               # « api.trycloudflare.com » n'est pas notre adresse
                    self.url = m.group(0)
        self.n_reg = 0
        if not self.url or (self.url_fixe and not self.erreur):
            fin = " ".join(x for x in self.lignes[-3:] if x)
            self.erreur = ("L'outil de connexion s'est arrêté sans donner d'adresse" + (f" : {fin[:160]}" if fin else ".") +
                           " Vérifiez qu'Internet fonctionne sur l'ordinateur.")

    def attendre(self, timeout=45):
        """Attend que l'adresse existe ET que la liaison avec Cloudflare soit établie. Retourne l'adresse, ou '' (voir self.erreur)."""
        fin = time.time() + timeout
        while time.time() < fin:
            if self.url and self.connecte:
                return self.url
            if self.proc and self.proc.poll() is not None:
                time.sleep(0.3)
                return self.url if (self.url and self.connecte) else ''
            time.sleep(0.2)
        cause = _diagnostic(self.lignes)
        if self.url and not self.connecte:
            self.erreur = ("L'adresse a été créée mais l'ordinateur n'arrive pas à se relier à Cloudflare (c'est ce qui provoque l'erreur 1033 sur le téléphone). "
                           + (cause or "Le réseau de l'ordinateur bloque probablement cette liaison.") + (f"  Dernier message : {self.lignes[-1][:140]}" if self.lignes else ""))
        else:
            self.erreur = self.erreur or ("Délai dépassé : aucune adresse reçue. " + (cause or "Internet est-il lent ou bloqué sur cet ordinateur ?"))
        return ''

    def ouvrir(self, delai=35, protocoles=('http2', 'quic')):
        """Démarre le tunnel et attend la liaison ; essaie un second mode de connexion si le premier échoue. Lève ErreurTunnel avec la cause."""
        erreurs = []
        for i, proto in enumerate(protocoles, 1):
            self.essai = f"essai {i}/{len(protocoles)} ({proto})"
            self.arreter()
            self.demarrer(proto)
            if self.attendre(delai):
                self.verifier_adresse()
                return self.url
            erreurs.append(self.erreur)
        self.arreter()
        raise ErreurTunnel(erreurs[0] if erreurs else "Connexion impossible.")

    def verifier_adresse(self, essais=6):
        """Contrôle depuis l'ordinateur que l'adresse répond ; patiente quelques secondes si Cloudflare propage encore l'adresse."""
        self.verification = ('inconnu', '')
        for _ in range(essais):
            self.verification = verifier(self.url)
            if self.verification[0] != 'pas_connecte':
                break
            time.sleep(2)
        return self.verification

    def actif(self):
        return bool(self.proc and self.proc.poll() is None and self.url and self.connecte)

    def arreter(self):
        p = self.proc
        if p and p.poll() is None:
            p.terminate()
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p.kill()
        self.url = ''
        self.n_reg = 0
