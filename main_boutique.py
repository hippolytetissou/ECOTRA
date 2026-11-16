# -*- coding: utf-8 -*-
"""
GESTION DE BOUTIQUE - Fichier principal : main_boutique.py   (lancer ce fichier)
Librairie, papeterie, fournitures scolaires, équipement de bureau.
"""
import csv
import math
import os
import sqlite3
import threading
import time
import traceback
import logging
import sys
import subprocess
import tkinter as tk
from tkinter import ttk, messagebox, simpledialog, filedialog
from tkinter import font as tkfont
import re
from datetime import date, datetime, timedelta

import database_boutique as D
import reseau_boutique as NET
try:
    from facture_boutique import make_ticket, recu_reglement, bon_depense, bon_reception, bon_ajustement, fiche_article, rapport_inventaire, journal_audit_pdf, installer_logo, apercu_entete
    _MANQUANTS = []
except ImportError:                                  # fichier oublié lors de la copie : expliqué au lancement
    make_ticket = recu_reglement = bon_depense = bon_reception = bon_ajustement = fiche_article = rapport_inventaire = journal_audit_pdf = installer_logo = apercu_entete = None
    _MANQUANTS = ['facture_boutique.py']
try:
    import mobile_boutique as MB
except ImportError:
    MB = None
    _MANQUANTS.append('mobile_boutique.py')
try:
    import tunnel_boutique as TUN
except ImportError:
    TUN = None
    _MANQUANTS.append('tunnel_boutique.py')
from database_boutique import Database, fcfa, to_fr, norm, MODES, REF_LABELS, ROLES, UNITES

try:
    from reportlab.pdfgen import canvas
    from reportlab.lib.units import mm
    from reportlab.graphics.barcode import code128
    HAS_PDF = True
except ImportError:
    HAS_PDF = False

FONT = 'Segoe UI' if sys.platform.startswith('win') else 'Arial'
THEME = {'bg': '#f3f5f9', 'card': '#ffffff', 'fg': '#1f2933', 'accent': '#2563eb', 'accent_h': '#1d4ed8', 'ok': '#16a34a',
         'ok_h': '#15803d', 'warn': '#ea580c', 'bad': '#dc2626', 'bad_h': '#b91c1c', 'head': '#0f2a52',
         'muted': '#64748b', 'line': '#d9e0ea', 'btn': '#e5eaf2', 'btn_h': '#d3dbe8', 'zebra': '#f6f8fb'}

# journal d'erreurs : une erreur inattendue ne doit jamais faire disparaître l'application en silence
try:
    os.makedirs(D.BASE_DIR, exist_ok=True)
    logging.basicConfig(filename=os.path.join(D.BASE_DIR, 'erreurs.log'), level=logging.ERROR,
                        format='%(asctime)s %(message)s', encoding='utf-8')
except (OSError, TypeError, ValueError):
    pass


# ============================================================ utilitaires UI
def num(txt, default=0.0):
    try:
        v = float(str(txt).replace(' ', '').replace('\u202f', '').replace('\xa0', '').replace(',', '.') or default)
    except ValueError:
        return default
    return v if math.isfinite(v) else default      # refuse « nan » et « inf »


def sort_key(v):
    """Clé de tri d'une cellule : nombres (« 12 500 FCFA », « 5 % ») et dates (« 09/11/2026 ») triés correctement, texte sans casse."""
    txt = str(v).strip()
    m = re.match(r'^(\d{2})/(\d{2})/(\d{4})$', txt)
    if m:
        return (0, int(m.group(3)) * 10000 + int(m.group(2)) * 100 + int(m.group(1)))
    brut = txt.replace('\u202f', '').replace('\xa0', '').replace('FCFA', '').replace('%', '').replace(' ', '').replace(',', '.')
    try:
        return (0, float(brut))
    except ValueError:
        return (1, txt.lower())


class Tree(ttk.Treeview):
    """Tableau : lignes alternées, colonnes qui s'élargissent selon le contenu, tri par clic sur l'en-tête."""
    _font = None

    def setup(self, cols, widths):
        self._ids = [c for c, _ in cols]
        self._labels = dict(cols)
        self._cw = dict(zip(self._ids, widths))
        self._asc = {}
        if Tree._font is None:
            Tree._font = tkfont.Font(family=FONT, size=10)
        for c, lab in cols:                                   # l'en-tête doit toujours rester lisible en entier
            self._grow(c, Tree._font.measure(lab) + 34)

    def _grow(self, col, need):
        need = min(int(need), 460)
        if need > self._cw.get(col, 0):
            self._cw[col] = need
            self.column(col, width=need)

    def insert(self, parent, index, iid=None, **kw):
        if not parent and len(self.get_children('')) % 2:
            kw['tags'] = tuple(kw.get('tags') or ()) + ('odd',)
        new = super().insert(parent, index, iid=iid, **kw) if iid is not None else super().insert(parent, index, **kw)
        vals = kw.get('values')
        if vals and getattr(self, '_ids', None):
            for c, v in zip(self._ids, vals):
                self._grow(c, Tree._font.measure(str(v)) + 26)
        return new

    def sort_by(self, col):
        asc = not self._asc.get(col, False)
        self._asc = {col: asc}
        rows = sorted(((sort_key(self.set(i, col)), i) for i in self.get_children('')), key=lambda x: x[0], reverse=not asc)
        for n, (_, i) in enumerate(rows):
            self.move(i, '', n)
            tags = self.item(i, 'tags')
            tags = tags.split() if isinstance(tags, str) else list(tags or ())
            tags = [t for t in tags if t != 'odd'] + (['odd'] if n % 2 else [])
            self.item(i, tags=tuple(tags))
        for c in self._ids:
            self.heading(c, text=self._labels[c] + ((' \u25b2' if asc else ' \u25bc') if c == col else ''))


def _autohide(bar, **grid):
    """Barre de défilement qui n'apparaît que lorsque le contenu dépasse."""
    def set_(lo, hi):
        if float(lo) <= 0.0 and float(hi) >= 1.0:
            bar.grid_remove()
        else:
            bar.grid(**grid)
        bar.set(lo, hi)
    return set_


def make_tree(parent, cols, widths, height=12):
    """Tableau avec barres de défilement verticale ET horizontale (visibles seulement si nécessaire).
    La hauteur demandée est volontairement petite : le tableau occupe la place disponible sans jamais pousser
    les boutons ou les totaux situés en dessous hors de l'écran."""
    f = ttk.Frame(parent)
    f.rowconfigure(0, weight=1); f.columnconfigure(0, weight=1)
    t = Tree(f, columns=[c for c, _ in cols], show='headings', height=min(height, 6))
    t.setup(cols, widths)
    principal = widths.index(max(widths))                     # seule cette colonne s'étire ; les autres gardent leur largeur
    for k, ((c, label), w) in enumerate(zip(cols, widths)):
        t.heading(c, text=label, command=lambda c=c: t.sort_by(c))
        t.column(c, width=t._cw[c], minwidth=60, stretch=(k == principal),
                 anchor='e' if label.startswith(('Prix', 'Stock', 'Qté', 'Total', 'Montant', 'Reste', 'Solde', 'PU', 'Début', 'Achats', 'Retours', 'Ventes', 'Pertes', 'Fin', 'Valeur')) else 'w')
    vsb = ttk.Scrollbar(f, orient='vertical', command=t.yview)
    hsb = ttk.Scrollbar(f, orient='horizontal', command=t.xview)
    t.configure(yscrollcommand=_autohide(vsb, row=0, column=1, sticky='ns'), xscrollcommand=_autohide(hsb, row=1, column=0, sticky='ew'))
    t.grid(row=0, column=0, sticky='nsew')
    vsb.grid(row=0, column=1, sticky='ns'); hsb.grid(row=1, column=0, sticky='ew')
    t.bind('<Shift-MouseWheel>', lambda e: t.xview_scroll(-1 if e.delta > 0 else 1, 'units'))
    t.bind('<Shift-Button-4>', lambda e: t.xview_scroll(-1, 'units')); t.bind('<Shift-Button-5>', lambda e: t.xview_scroll(1, 'units'))
    t.frame = f
    t.tag_configure('odd', background=THEME['zebra'])
    t.tag_configure('bas', foreground=THEME['warn'])
    t.tag_configure('rupture', foreground=THEME['bad'])
    t.tag_configure('annule', foreground='#9e9e9e')
    t.tag_configure('dette', foreground=THEME['bad'])
    return t


class ScrollFrame(ttk.Frame):
    """Page à défilement vertical : si le contenu dépasse la fenêtre, une barre coulissante apparaît (molette comprise).
    Le contenu se place dans .inner."""
    def __init__(self, parent, padding=0, **kw):
        super().__init__(parent, **kw)
        self.canvas = tk.Canvas(self, highlightthickness=0, bg=THEME['bg'])
        self.vsb = ttk.Scrollbar(self, orient='vertical', command=self.canvas.yview)
        self.inner = ttk.Frame(self.canvas, padding=padding)
        self._fenetre = self.canvas.create_window((0, 0), window=self.inner, anchor='nw')
        self.canvas.configure(yscrollcommand=self._maj_barre)
        self.canvas.pack(side='left', fill='both', expand=True)
        self.inner.bind('<Configure>', lambda e: self.canvas.configure(scrollregion=self.canvas.bbox('all')))
        self.canvas.bind('<Configure>', lambda e: self.canvas.itemconfigure(self._fenetre, width=e.width))
        self.bind('<Enter>', self._bind_wheel); self.bind('<Leave>', self._unbind_wheel)

    def _maj_barre(self, lo, hi):
        if float(lo) <= 0.0 and float(hi) >= 1.0:
            self.vsb.pack_forget()
        else:
            self.vsb.pack(side='right', fill='y')
        self.vsb.set(lo, hi)

    def _bind_wheel(self, _=None):
        self.canvas.bind_all('<MouseWheel>', self._wheel)
        self.canvas.bind_all('<Button-4>', lambda e: self._wheel(e, 1)); self.canvas.bind_all('<Button-5>', lambda e: self._wheel(e, -1))

    def _unbind_wheel(self, _=None):
        for ev in ('<MouseWheel>', '<Button-4>', '<Button-5>'):
            self.canvas.unbind_all(ev)

    def _wheel(self, e, sens=None):
        if e.widget.winfo_class() in ('Treeview', 'Text', 'TCombobox', 'Listbox'):     # ces zones défilent déjà seules
            return
        sens = sens if sens is not None else (1 if e.delta > 0 else -1)
        if self.vsb.winfo_ismapped():
            self.canvas.yview_scroll(-sens, 'units')


def clear(t):
    t.delete(*t.get_children())


def ask_reason(parent, title, prompt):
    return simpledialog.askstring(title, prompt, parent=parent)


def export_csv(parent, header, rows, name):
    path = filedialog.asksaveasfilename(parent=parent, defaultextension='.csv', initialfile=name, filetypes=[('CSV (Excel)', '*.csv')])
    if not path:
        return
    with open(path, 'w', newline='', encoding='utf-8-sig') as f:
        w = csv.writer(f, delimiter=';')
        w.writerow(header)
        w.writerows(rows)
    messagebox.showinfo("Export", f"Fichier enregistré :\n{path}", parent=parent)


def open_file(path):
    try:
        if sys.platform.startswith('win'):
            os.startfile(path)
        elif sys.platform == 'darwin':
            subprocess.Popen(['open', path])
        else:
            subprocess.Popen(['xdg-open', path])
    except OSError:
        messagebox.showinfo("Fichier créé", path)


def apply_theme(root):
    s = ttk.Style(root)
    try:
        s.theme_use('clam')
    except tk.TclError:
        pass
    T = THEME
    root.configure(bg=T['bg'])
    s.configure('.', font=(FONT, 10), background=T['bg'], foreground=T['fg'], bordercolor=T['line'], focuscolor=T['accent'])
    s.configure('TFrame', background=T['bg'])
    s.configure('TLabel', background=T['bg'])
    s.configure('Title.TLabel', font=(FONT, 15, 'bold'), foreground=T['head'])
    s.configure('Big.TLabel', font=(FONT, 24, 'bold'), foreground=T['accent'])
    s.configure('Muted.TLabel', foreground=T['muted'])
    s.configure('Status.TLabel', background=T['head'], foreground='white', padding=(10, 4))
    # tableaux
    s.configure('Treeview', rowheight=28, background='white', fieldbackground='white', bordercolor=T['line'], borderwidth=1)
    s.map('Treeview', background=[('selected', T['accent'])], foreground=[('selected', 'white')])
    s.configure('Treeview.Heading', font=(FONT, 10, 'bold'), background='#e3e9f2', foreground=T['head'], relief='flat', padding=(6, 6))
    s.map('Treeview.Heading', background=[('active', '#d3dbe8')])
    # champs
    s.configure('TEntry', padding=6, fieldbackground='white', bordercolor=T['line'])
    s.map('TEntry', bordercolor=[('focus', T['accent'])])
    s.configure('TCombobox', padding=5, fieldbackground='white')
    # onglets
    s.configure('TNotebook', background=T['bg'], borderwidth=0, tabmargins=(0, 4, 0, 0))
    s.configure('TNotebook.Tab', padding=(18, 9), font=(FONT, 10, 'bold'), background='#e3e9f2', foreground=T['muted'], borderwidth=0)
    s.map('TNotebook.Tab', background=[('selected', 'white'), ('active', '#d3dbe8')], foreground=[('selected', T['accent'])])
    # boutons : plats, arrondis visuellement par le padding, couleur au survol
    base = dict(font=(FONT, 10, 'bold'), padding=(16, 8), relief='flat', borderwidth=0, anchor='center')
    for name, bg, hov, fg in (('Soft', T['btn'], T['btn_h'], T['fg']), ('Accent', T['accent'], T['accent_h'], 'white'),
                              ('Ok', T['ok'], T['ok_h'], 'white'), ('Danger', T['bad'], T['bad_h'], 'white')):
        st = name + '.TButton'
        s.configure(st, background=bg, foreground=fg, **base)
        s.map(st, background=[('disabled', '#e5e7eb'), ('pressed', hov), ('active', hov)],
              foreground=[('disabled', '#9ca3af')], bordercolor=[('focus', T['head'])])
    s.configure('Ok.TButton', font=(FONT, 12, 'bold'), padding=(16, 10))
    s.configure('TButton', **base)
    s.configure('TCheckbutton', background=T['bg'])
    for o in ('Vertical', 'Horizontal'):
        s.configure(f'{o}.TScrollbar', background='#c3cddd', troughcolor='#eef1f6', bordercolor='#eef1f6', lightcolor='#c3cddd',
                    darkcolor='#c3cddd', arrowcolor=T['muted'], gripcount=0, arrowsize=14, relief='flat')
        s.map(f'{o}.TScrollbar', background=[('active', '#9fb0cc'), ('pressed', '#8095b8')])
    # cartes blanches
    s.configure('Card.TFrame', background='white')
    s.configure('Card.TLabel', background='white')
    s.configure('CardMuted.TLabel', background='white', foreground=T['muted'])
    s.configure('CardTitle.TLabel', background='white', foreground=T['head'], font=(FONT, 11, 'bold'))
    s.configure('Total.TLabel', background=T['head'], foreground='white', font=(FONT, 26, 'bold'))
    s.configure('TotalCap.TLabel', background=T['head'], foreground='#cbd5e1', font=(FONT, 9, 'bold'))
    s.configure('Ghost.TButton', background=T['head'], foreground='white', **base)
    s.map('Ghost.TButton', background=[('active', '#1e3f73'), ('pressed', '#1e3f73')])
    s.configure('TPanedwindow', background=T['bg'])


# ============================================================ boutons modernes
_KINDS = (('Danger', ('annuler', 'supprimer', 'archiver', 'retirer', 'vider', 'quitter', 'déconnecter')),
          ('Ok', ('encaisser', 'valider', 'clôturer', 'cloturer')),
          ('Accent', ('enregistrer', 'ajouter', 'nouveau', 'nouvelle', 'créer', 'choisir', 'se connecter', 'réception', 'recevoir',
                      'payer', 'régler', 'saisir', 'importer', 'ok')))
_ICONS = {'Danger': '\u2715  ', 'Ok': '\u2713  '}


def _kind_for(text):
    t = (text or '').lower().strip()
    for kind, words in _KINDS:
        if any(t.startswith(w) for w in words):
            return kind
    return None


def card(parent, pad=12):
    """Carte blanche à fine bordure. Le contenu se place dans card.inner."""
    outer = tk.Frame(parent, bg='white', highlightbackground=THEME['line'], highlightcolor=THEME['line'], highlightthickness=1)
    outer.inner = ttk.Frame(outer, style='Card.TFrame', padding=pad)
    outer.inner.pack(fill='both', expand=True)
    return outer


JOURS = ['lun.', 'mar.', 'mer.', 'jeu.', 'ven.', 'sam.', 'dim.']
MOIS = ['janvier', 'février', 'mars', 'avril', 'mai', 'juin', 'juillet', 'août', 'septembre', 'octobre', 'novembre', 'décembre']


def date_longue(d=None):
    d = d or date.today()
    return f"{['lundi', 'mardi', 'mercredi', 'jeudi', 'vendredi', 'samedi', 'dimanche'][d.weekday()]} {d.day} {MOIS[d.month - 1]} {d.year}"


def court(v):
    """12 500 -> « 12,5 k » ; 2 300 000 -> « 2,3 M » (étiquettes de graphique)."""
    v = float(v or 0)
    if v >= 1_000_000:
        return f"{v / 1_000_000:.1f}".replace('.', ',').rstrip('0').rstrip(',') + " M"
    if v >= 1000:
        return f"{v / 1000:.1f}".replace('.', ',').rstrip('0').rstrip(',') + " k"
    return f"{v:.0f}"


class Flow(ttk.Frame):
    """Barre de boutons qui passe à la ligne quand la fenêtre est étroite : aucun bouton n'est jamais coupé."""
    def __init__(self, parent, **kw):
        super().__init__(parent, **kw)
        self._kids, self._larg = [], 0
        self.bind('<Configure>', self._on_conf)

    def add(self, w):
        self._kids.append(w)
        self._layout()

    def clear(self):
        for w in self._kids:
            try:
                w.destroy()
            except tk.TclError:
                pass
        self._kids = []

    def _on_conf(self, e):
        if e.width != self._larg:
            self._larg = e.width
            self._layout()

    def _layout(self):
        largeur = self._larg if self._larg > 1 else 10 ** 6
        x = ligne = col = 0
        for w in self._kids:
            lw = w.winfo_reqwidth() + 8
            if x and x + lw > largeur:
                ligne, col, x = ligne + 1, 0, 0
            w.grid(row=ligne, column=col, padx=(0, 8), pady=(0, 4), sticky='w')
            col += 1
            x += lw


class Button(ttk.Button):
    """Bouton moderne : couleur selon l'action (danger / validation / principale), survol, curseur main, focus clavier."""
    def __init__(self, parent, text='', style=None, **kw):
        kind = None
        if style is None:
            kind = _kind_for(text)
            style = (kind + '.TButton') if kind else 'Soft.TButton'
        if kind in _ICONS and text and not text[0] in '\u2715\u2713':
            text = _ICONS[kind] + text
        kw.setdefault('cursor', 'hand2')
        kw.setdefault('takefocus', True)
        super().__init__(parent, text=text, style=style, **kw)
        self.bind('<Return>', lambda e: self.invoke() if str(self['state']) != 'disabled' else None)

    def pack(self, *a, **kw):
        if isinstance(self.master, Flow):          # dans une barre « Flow », la mise en page est automatique
            self.master.add(self)
            return None
        return super().pack(*a, **kw)


class Dialog(tk.Toplevel):
    """Fenêtre modale : centrée sur la fenêtre parente, fermée par Échap."""
    def __init__(self, parent, *a, **k):
        super().__init__(parent, *a, **k)
        self.configure(bg=THEME['bg'])
        self.bind('<Escape>', lambda e: self.destroy())
        self.after(30, self._center)

    def grab_set(self):
        try:
            self.wait_visibility()
        except tk.TclError:
            pass
        try:
            super().grab_set()
        except tk.TclError:
            pass

    def _center(self):
        try:
            self.update_idletasks()
            p = self.master.winfo_toplevel()
            sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
            # la fenêtre s'agrandit si son contenu (boutons du bas) ne tient pas dans la taille prévue
            w = min(max(self.winfo_width(), self.winfo_reqwidth()), sw - 60)
            h = min(max(self.winfo_height(), self.winfo_reqheight()), sh - 100)
            x = max(0, min(p.winfo_rootx() + (p.winfo_width() - w) // 2, sw - w))
            y = max(0, min(p.winfo_rooty() + (p.winfo_height() - h) // 3, sh - h - 50))
            self.geometry(f"{w}x{h}+{x}+{y}")
            self.minsize(min(self.winfo_reqwidth(), sw - 60), min(self.winfo_reqheight(), sh - 100))
        except tk.TclError:
            pass


def recu_auto(parent, fabrique, *args, **kw):
    """Génère automatiquement le document d'une opération (reçu, bon...) et l'ouvre si le réglage l'autorise.
    L'opération est déjà enregistrée : un problème d'impression ne doit jamais la remettre en cause."""
    forcer = kw.pop('forcer_ouverture', False)                 # états (inventaire, journal) : toujours ouverts
    try:
        if fabrique is None:
            raise RuntimeError("le fichier facture_boutique.py est absent du dossier")
        chemin = fabrique(*args, **kw)
        if forcer or str(D.get_config().get('ouvrir_recus', 'oui')).lower() != 'non':
            open_file(chemin)
        return chemin
    except Exception as e:
        logging.error("Document automatique impossible", exc_info=True)
        try:
            messagebox.showwarning("Document", f"L'opération est bien enregistrée, mais le document n'a pas pu être créé :\n{e}", parent=parent)
        except tk.TclError:
            pass
        return None


def periode_ok(parent, d1, d2):
    """Vérifie une période saisie (AAAA-MM-JJ) avant de la transmettre à la base."""
    try:
        a, b = datetime.strptime(d1, '%Y-%m-%d'), datetime.strptime(d2, '%Y-%m-%d')
    except ValueError:
        messagebox.showerror("Dates", "Dates invalides : utilisez le format AAAA-MM-JJ (par exemple 2026-11-30).", parent=parent)
        return False
    if a > b:
        messagebox.showerror("Dates", "La date de début doit précéder la date de fin.", parent=parent)
        return False
    return True


def friendly_error(parent, exc):
    """Message clair pour l'utilisateur ; le détail technique va dans erreurs.log."""
    if isinstance(exc, PermissionError):
        title, msg = "Droits insuffisants", str(exc) or "Votre profil n'autorise pas cette action."
    elif isinstance(exc, (NET.NetworkError, NET.SessionError)):
        title, msg = "Réseau", f"{exc}\n\nVérifiez que le poste serveur est allumé et que le câble / le Wi-Fi fonctionne, puis réessayez. Aucune donnée n'a été perdue."
    elif isinstance(exc, sqlite3.OperationalError) and 'locked' in str(exc).lower():
        title, msg = "Base occupée", "La base de données est utilisée par un autre poste. Patientez quelques secondes et réessayez."
    elif isinstance(exc, sqlite3.Error):
        logging.error("Erreur base de données", exc_info=exc)
        title, msg = "Base de données", "Une erreur est survenue dans la base de données. L'opération a été annulée.\nSi le problème se répète, contactez le support (détail : erreurs.log)."
    else:
        logging.error("Erreur inattendue", exc_info=exc)
        title, msg = "Erreur", f"Une erreur inattendue est survenue :\n{exc}\n\nL'application reste ouverte (détail : erreurs.log)."
    try:
        messagebox.showerror(title, msg, parent=parent)
    except tk.TclError:
        pass


# ============================================================ reçus / factures
def make_labels(db, items, path):
    """Planche d'étiquettes A4 (3 x 8) avec code-barres Code 128. items : [(article, nombre_de_copies)]."""
    cfg = D.get_config()
    W, H = 210 * mm, 297 * mm
    cols, rows = 3, 8
    lw, lh = W / cols, H / rows
    c = canvas.Canvas(path, pagesize=(W, H))
    n = 0
    for a, copies in items:
        value = (a['isbn'] or a['code']).strip()
        for _ in range(int(copies)):
            i = n % (cols * rows)
            if n and i == 0:
                c.showPage()
            x, y = (i % cols) * lw, H - ((i // cols) + 1) * lh
            c.setStrokeColorRGB(.8, .8, .8); c.rect(x + 1 * mm, y + 1 * mm, lw - 2 * mm, lh - 2 * mm)
            c.setFillColorRGB(0, 0, 0)
            c.setFont('Helvetica', 6); c.drawCentredString(x + lw / 2, y + lh - 5 * mm, cfg['boutique'][:40])
            c.setFont('Helvetica-Bold', 8)
            des = a['designation']
            c.drawCentredString(x + lw / 2, y + lh - 9.5 * mm, des[:34])
            if len(des) > 34:
                c.setFont('Helvetica', 7); c.drawCentredString(x + lw / 2, y + lh - 12.5 * mm, des[34:72])
            bw = 0.33 * mm
            bc = code128.Code128(value, barWidth=bw, barHeight=11 * mm, quiet=False)
            while bc.width > lw - 8 * mm and bw > 0.15 * mm:
                bw -= 0.02 * mm; bc = code128.Code128(value, barWidth=bw, barHeight=11 * mm, quiet=False)
            bc.drawOn(c, x + (lw - bc.width) / 2, y + 8 * mm)
            c.setFont('Helvetica', 6); c.drawCentredString(x + lw / 2, y + 5.3 * mm, value)
            c.setFont('Helvetica-Bold', 11); c.drawCentredString(x + lw / 2, y + 2 * mm, fcfa(a['prix_vente']))
            n += 1
    c.save()
    return path


# ============================================================ onglet VENTE (caisse)
class VenteTab:
    def __init__(self, nb, app):
        self.app, self.db = app, app.db
        self.outer = ttk.Frame(nb, padding=10)
        self.cart = []                        # dict(id, des, qte, pu, rem, stock)
        self.client_id = None
        self.pays = []                        # paiements déjà saisis : (mode, montant, référence)
        self._busy = False
        self.held = self._load_held()         # ventes mises en attente (conservées même si le logiciel se ferme)
        # --- barre de recherche
        top = card(self.outer, 8); top.pack(fill='x')
        t = top.inner
        ttk.Label(t, text="Scanner / chercher  (F2)", style='CardTitle.TLabel').pack(side='left')
        self.q = tk.StringVar()
        self.entry = ttk.Entry(t, textvariable=self.q, width=42, font=(FONT, 13))
        self.entry.pack(side='left', padx=10)
        self.entry.bind('<Return>', self.on_enter)
        self.entry.bind('<Down>', lambda e: self._focus_results())
        self.q.trace_add('write', lambda *_: self.refresh_results())
        self.entry.focus_set()
        Button(t, text="Vider le panier", command=self.reset).pack(side='right')
        Button(t, text="Pack scolaire...", command=self.add_pack).pack(side='right', padx=6)
        body = ttk.PanedWindow(self.outer, orient='horizontal'); body.pack(fill='both', expand=True, pady=8)
        left = ttk.Frame(body); right = ttk.Frame(body, padding=(10, 0, 0, 0))
        body.add(left, weight=3); body.add(right, weight=2)
        ttk.Label(left, text="Résultats  (double-clic ou Entrée = ajouter)", style='Title.TLabel').pack(anchor='w', pady=(0, 3))
        self.res = make_tree(left, [('des', 'Désignation'), ('cat', 'Catégorie'), ('pv', 'Prix'), ('st', 'Stock')], [300, 160, 80, 60], 6)
        self.res.frame.pack(fill='both', expand=True)
        self.res.bind('<Double-1>', lambda e: self.add_selected())
        self.res.bind('<Return>', lambda e: self.add_selected())
        ttk.Label(left, text="Panier  (double-clic : quantité)", style='Title.TLabel').pack(anchor='w', pady=(10, 3))
        self.tv = make_tree(left, [('des', 'Désignation'), ('q', 'Qté'), ('pu', 'PU'), ('rem', 'Remise %'), ('tot', 'Total')], [260, 50, 80, 70, 90], 6)
        self.tv.frame.pack(fill='both', expand=True)
        self.tv.bind('<Double-1>', lambda e: self.edit_qty())
        self.tv.bind('<Delete>', lambda e: self.remove())
        bar = ttk.Frame(left); bar.pack(fill='x', pady=6)
        for txt, fn in (("Quantité", self.edit_qty), ("Remise ligne", self.edit_rem), ("Retirer", self.remove)):
            Button(bar, text=txt, command=fn).pack(side='left', padx=(0, 6))
        # --- panneau de paiement : les boutons d'action sont ancrés en bas, donc toujours visibles, même sur un petit écran
        pay = card(right, 0); pay.pack(fill='both', expand=True)
        P = pay.inner; P.configure(padding=0)
        foot = ttk.Frame(P, style='Card.TFrame', padding=(14, 8, 14, 14)); foot.pack(side='bottom', fill='x')
        hb = ttk.Frame(foot, style='Card.TFrame'); hb.pack(fill='x')
        Button(hb, text="Mettre en attente  (F6)", command=self.hold).pack(side='left', fill='x', expand=True, padx=(0, 4))
        self.btn_resume = Button(hb, text="Reprendre  (F7)", command=self.resume); self.btn_resume.pack(side='left', fill='x', expand=True, padx=(4, 0))
        Button(foot, text="\u2713  ENCAISSER  (F9)", style='Ok.TButton', command=self.valider).pack(fill='x', pady=(8, 0))
        tot = tk.Frame(P, bg=THEME['head']); tot.pack(side='top', fill='x')
        ttk.Label(tot, text="TOTAL À PAYER", style='TotalCap.TLabel').pack(anchor='w', padx=16, pady=(10, 0))
        self.lbl_total = ttk.Label(tot, text="0 FCFA", style='Total.TLabel'); self.lbl_total.pack(anchor='w', padx=16, pady=(0, 10))
        body_p = ttk.Frame(P, style='Card.TFrame', padding=(14, 10)); body_p.pack(side='top', fill='both', expand=True)
        g = ttk.Frame(body_p, style='Card.TFrame'); g.pack(fill='x')
        g.columnconfigure(1, weight=1)
        L = lambda txt, r: ttk.Label(g, text=txt, style='CardMuted.TLabel').grid(row=r, column=0, sticky='w', pady=3)
        L("Remise globale %", 0)
        self.rg = tk.StringVar(value="0"); ttk.Entry(g, textvariable=self.rg, width=8).grid(row=0, column=1, sticky='w', padx=8)
        self.rg.trace_add('write', lambda *_: self.recalc())
        L("Client", 1)
        self.lbl_client = ttk.Label(g, text="Client de passage", style='Card.TLabel', font=(FONT, 10, 'bold')); self.lbl_client.grid(row=1, column=1, sticky='w', padx=8)
        Button(g, text="Choisir...", command=self.pick_client).grid(row=1, column=2, sticky='e')
        ttk.Separator(body_p).pack(fill='x', pady=6)
        ttk.Label(body_p, text="Paiement", style='CardTitle.TLabel').pack(anchor='w')
        g2 = ttk.Frame(body_p, style='Card.TFrame'); g2.pack(fill='x', pady=(2, 0)); g2.columnconfigure(1, weight=1)
        L2 = lambda txt, r: ttk.Label(g2, text=txt, style='CardMuted.TLabel').grid(row=r, column=0, sticky='w', pady=2)
        L2("Mode", 0)
        self.mode = tk.StringVar(value="Espèces")
        ttk.Combobox(g2, textvariable=self.mode, values=[m for m in MODES if m != "Crédit client"], state='readonly', width=16).grid(row=0, column=1, sticky='w', padx=8)
        L2("Référence", 1)
        self.ref = tk.StringVar(); ttk.Entry(g2, textvariable=self.ref, width=22).grid(row=1, column=1, sticky='w', padx=8)
        L2("Montant", 2)
        self.recu = tk.StringVar(); er = ttk.Entry(g2, textvariable=self.recu, width=14, font=(FONT, 13)); er.grid(row=2, column=1, sticky='w', padx=8)
        er.bind('<Return>', lambda e: self.valider())
        self.recu.trace_add('write', lambda *_: self.recalc())
        Button(g2, text="+ Ajouter", command=self.add_payment).grid(row=2, column=2, sticky='e')
        self.tp = make_tree(body_p, [('mode', 'Mode'), ('montant', 'Montant'), ('ref', 'Référence')], [110, 90, 120], 2)
        self.tp.frame.pack(fill='x', pady=(6, 0))
        self.tp.bind('<Delete>', lambda e: self.remove_payment()); self.tp.bind('<Double-1>', lambda e: self.remove_payment())
        self.lbl_rendu = ttk.Label(body_p, text="", style='Card.TLabel', font=(FONT, 12, 'bold')); self.lbl_rendu.pack(anchor='w', pady=4)
        ttk.Label(body_p, text="Plusieurs modes : saisissez un paiement puis « + Ajouter ». Reste non payé = crédit (client requis).",
                  style='CardMuted.TLabel', wraplength=360, justify='left').pack(anchor='w')
        top_w = self.outer.winfo_toplevel()
        top_w.bind('<F9>', self._f9); top_w.bind('<F2>', lambda e: self._focus_search())
        top_w.bind('<F6>', lambda e: self._if_active(self.hold)); top_w.bind('<F7>', lambda e: self._if_active(self.resume))
        self.upd_hold()
        self.refresh_results()

    # --- touches de fonction : actives seulement sur cet onglet et sans fenêtre ouverte
    def _active(self):
        try:
            return self.outer.winfo_toplevel().nametowidget(self.app.nb.select()) is self.outer
        except (tk.TclError, KeyError, AttributeError):
            return False

    def _if_active(self, fn):
        if self._active() and not isinstance(self.outer.grab_current(), tk.Toplevel):
            fn()

    def _f9(self, _=None):
        self._if_active(self.valider)

    def _focus_search(self):
        if self._active():
            self.entry.focus_set(); self.entry.selection_range(0, 'end')

    def _focus_results(self):
        kids = self.res.get_children()
        if kids:
            self.res.focus_set(); self.res.selection_set(kids[0]); self.res.focus(kids[0])

    # --- recherche
    def refresh_results(self):
        clear(self.res)
        term = self.q.get().strip()
        for a in self.db.search_articles(term, limit=60 if term else 30):
            tag = 'rupture' if a['stock'] <= 0 else ('bas' if a['stock'] <= a['seuil_alerte'] else '')
            self.res.insert('', 'end', iid=str(a['id']), values=(a['designation'], a['categorie'], fcfa(a['prix_vente']).replace(' FCFA', ''), f"{a['stock']:g}"), tags=(tag,))

    def on_enter(self, _=None):
        term = self.q.get().strip()
        if not term:
            return
        a = self.db.find_code(term)       # lecteur de code-barres : correspondance exacte => ajout direct
        if a:
            self.add(a['id']); self.q.set(''); return
        kids = self.res.get_children()
        if len(kids) == 1:
            self.add(int(kids[0])); self.q.set('')

    def add_selected(self):
        s = self.res.selection()
        if s:
            self.add(int(s[0]))

    def add(self, aid, qte=1, quiet=False):
        """Ajoute au panier. Retourne la quantité réellement ajoutée (limitée par le stock)."""
        a = self.db.get_article(aid)
        if a['stock'] <= 0:
            if not quiet:
                messagebox.showwarning("Rupture de stock", f"« {a['designation']} » n'est plus en stock.", parent=self.outer)
            return 0
        line = next((l for l in self.cart if l['id'] == aid), None)
        deja = line['qte'] if line else 0
        ajout = min(qte, a['stock'] - deja)
        if ajout <= 0:
            if not quiet:
                messagebox.showwarning("Stock", f"Stock disponible : {a['stock']:g}", parent=self.outer)
            return 0
        if line:
            line['qte'] += ajout
        else:
            self.cart.append(dict(id=aid, des=a['designation'], qte=ajout, pu=a['prix_vente'], rem=0, stock=a['stock']))
        if not quiet:
            self.render()
        return ajout

    def add_pack(self):
        PackManager(self.outer, self.db, select=self.apply_pack)

    def apply_pack(self, pack):
        manques = []
        for l in self.db.get_pack_lignes(pack['id']):
            if not l['actif']:
                manques.append(f"{l['designation']} (archivé)"); continue
            fait = self.add(l['article_id'], l['quantite'], quiet=True)
            if fait < l['quantite']:
                manques.append(f"{l['designation']} : {fait:g}/{l['quantite']:g}")
        self.render()
        if manques:
            messagebox.showwarning("Pack incomplet", "Stock insuffisant pour :\n- " + "\n- ".join(manques), parent=self.outer)

    def selected_line(self):
        s = self.tv.selection()
        return self.cart[int(s[0])] if s else None

    def edit_qty(self):
        l = self.selected_line()
        if l:
            v = simpledialog.askfloat("Quantité", f"{l['des']}\nStock disponible : {l['stock']:g}", initialvalue=l['qte'], minvalue=0.01, parent=self.outer)
            if v:
                l['qte'] = min(v, l['stock']); self.render()

    def edit_rem(self):
        l = self.selected_line()
        if l:
            v = simpledialog.askfloat("Remise", "Remise sur cette ligne (%) :", initialvalue=l['rem'], minvalue=0, maxvalue=100, parent=self.outer)
            if v is not None:
                l['rem'] = v; self.render()

    def remove(self):
        s = self.tv.selection()
        if s:
            del self.cart[int(s[0])]; self.render()

    def reset(self):
        self.cart, self.client_id, self.pays = [], None, []
        self.lbl_client.config(text="Client de passage")
        self.rg.set("0"); self.recu.set(""); self.ref.set("")
        self.render(); self.render_pays(); self.entry.focus_set()

    def render(self):
        clear(self.tv)
        for i, l in enumerate(self.cart):
            tot = round(l['qte'] * l['pu'] * (1 - l['rem'] / 100))
            self.tv.insert('', 'end', iid=str(i), values=(l['des'], f"{l['qte']:g}", f"{l['pu']:,.0f}".replace(',', ' '), f"{l['rem']:g}", f"{tot:,.0f}".replace(',', ' ')))
        self.recalc()

    def total(self):
        sous = sum(round(l['qte'] * l['pu'] * (1 - l['rem'] / 100)) for l in self.cart)
        rem = round(sous * min(max(num(self.rg.get()), 0), 100) / 100)
        tva = round((sous - rem) * D.get_config()['tva_taux'] / 100)
        return sous - rem + tva

    # --- paiement mixte
    def paid_list(self):
        return sum(p[1] for p in self.pays)

    def current_amount(self, t):
        """Montant du paiement en cours de saisie : ce qui est tapé, sinon tout ce qui reste à payer. None si invalide."""
        txt = self.recu.get().strip()
        if not txt:
            return max(t - self.paid_list(), 0)
        v = num(txt, -1)
        return None if v < 0 else v

    def add_payment(self):
        t = self.total()
        if t <= 0:
            return
        cur = self.current_amount(t)
        if cur is None or cur <= 0:
            messagebox.showwarning("Paiement", "Saisissez un montant supérieur à 0.", parent=self.outer); return
        restant = t - self.paid_list()
        if self.mode.get() != "Espèces" and cur > restant + 0.001:
            messagebox.showwarning("Paiement", f"Ce mode ne peut pas dépasser le reste à payer ({fcfa(restant)}).", parent=self.outer); return
        if self.mode.get() != "Espèces" and not self.ref.get().strip():
            messagebox.showwarning("Paiement", "Indiquez la référence de la transaction pour ce mode de paiement.", parent=self.outer); return
        self.pays.append((self.mode.get(), cur, self.ref.get().strip()))
        self.recu.set(""); self.ref.set("")
        self.render_pays(); self.recalc()

    def remove_payment(self):
        s = self.tp.selection()
        if s:
            del self.pays[int(s[0])]; self.render_pays(); self.recalc()

    def render_pays(self):
        clear(self.tp)
        for i, (m, a, r) in enumerate(self.pays):
            self.tp.insert('', 'end', iid=str(i), values=(m, f"{a:,.0f}".replace(',', ' '), r))

    def recalc(self):
        t = self.total()
        self.lbl_total.config(text=fcfa(t))
        cur = self.current_amount(t)
        if t <= 0 or cur is None:
            self.lbl_rendu.config(text="")
            return
        somme = self.paid_list() + cur
        if somme >= t - 0.001:
            exces = somme - t
            self.lbl_rendu.config(text=(f"Monnaie à rendre : {fcfa(exces)}" if exces > 0.001 else "Montant exact"), foreground=THEME['ok'])
        else:
            self.lbl_rendu.config(text=f"Reste à crédit : {fcfa(t - somme)}", foreground=THEME['warn'])

    # --- mise en attente
    def _held_path(self):
        return os.path.join(D.BASE_DIR, f"attentes_{self.app.user['username']}.json")

    def _load_held(self):
        try:
            import json
            with open(self._held_path(), encoding='utf-8') as f:
                data = json.load(f)
            return [h for h in data if h.get('jour') == date.today().isoformat() and h.get('cart')]   # le stock n'est pas réservé : on ne garde que la journée
        except (OSError, ValueError, TypeError, AttributeError):
            return []

    def _save_held(self):
        import json
        try:
            tmp = self._held_path() + '.part'
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(self.held, f, ensure_ascii=False)
            os.replace(tmp, self._held_path())
        except OSError:
            logging.error("Impossible d'enregistrer les ventes en attente", exc_info=True)

    def upd_hold(self):
        self.btn_resume.config(text=f"Reprendre ({len(self.held)})  (F7)" if self.held else "Reprendre  (F7)")

    def hold(self, auto=False):
        if not self.cart:
            if not auto:
                messagebox.showinfo("Mise en attente", "Le panier est vide.", parent=self.outer)
            return
        if len(self.held) >= 20:
            messagebox.showwarning("Mise en attente", "20 ventes sont déjà en attente : reprenez-en ou supprimez-en une.", parent=self.outer); return
        self.held.append(dict(jour=date.today().isoformat(), heure=datetime.now().strftime('%H:%M'), cart=[dict(l) for l in self.cart],
                              client_id=self.client_id, client=self.lbl_client.cget('text') if self.client_id else '',
                              rg=self.rg.get(), pays=[list(p) for p in self.pays], total=self.total()))
        self._save_held(); self.upd_hold()
        if not auto:
            self.reset()

    def resume(self):
        if not self.held:
            messagebox.showinfo("Reprendre", "Aucune vente en attente.", parent=self.outer); return
        AttentesDialog(self.outer, self)

    def restore_held(self, idx):
        """Recharge une vente en attente. Stock et prix sont revérifiés : le stock a pu changer entre-temps."""
        e = self.held[idx]
        if self.cart:
            self.hold(auto=True)                       # le panier en cours passe en attente, rien n'est perdu
            idx = self.held.index(e)
        self.held.remove(e); self._save_held(); self.upd_hold()
        self.cart, self.pays, notes = [], [], []
        for l in e['cart']:
            a = self.db.get_article(l['id'])
            if not a or not a['actif']:
                notes.append(f"{l['des']} : article retiré du catalogue"); continue
            q = min(l['qte'], a['stock'])
            if q <= 0:
                notes.append(f"{l['des']} : plus en stock"); continue
            if q < l['qte']:
                notes.append(f"{l['des']} : {q:g} sur {l['qte']:g} (stock)")
            self.cart.append(dict(id=l['id'], des=a['designation'], qte=q, pu=max(l['pu'], a['prix_vente']), rem=l['rem'], stock=a['stock']))
        self.client_id = e.get('client_id')
        self.lbl_client.config(text=e.get('client') or "Client de passage")
        self.rg.set(e.get('rg', "0")); self.pays = [tuple(p) for p in e.get('pays', [])]
        self.recu.set(""); self.ref.set("")
        self.render(); self.render_pays()
        if notes:
            messagebox.showwarning("Vente reprise", "Certaines lignes ont changé :\n- " + "\n- ".join(notes), parent=self.outer)

    def delete_held(self, idx):
        del self.held[idx]; self._save_held(); self.upd_hold()

    # --- client et validation
    def pick_client(self):
        ClientPicker(self.outer, self.db, self.set_client)

    def set_client(self, c):
        self.client_id = c['id'] if c else None
        self.lbl_client.config(text=c['nom'] if c else "Client de passage")

    def valider(self):
        if not self.cart or self._busy:
            return
        self._busy = True
        try:
            self._valider()
        except Exception as e:           # réseau coupé, base occupée... : message clair, panier conservé
            friendly_error(self.outer, e)
        finally:
            self._busy = False

    def _valider(self):
        t = self.total()
        cur = self.current_amount(t)
        if cur is None:
            messagebox.showerror("Montant", "Le montant saisi est invalide.", parent=self.outer); return
        paiements = list(self.pays) + ([(self.mode.get(), cur, self.ref.get().strip())] if cur > 0 else [])
        regs, rendu, err = D.plan_payments(t, paiements)
        if err:
            messagebox.showerror("Paiement", err, parent=self.outer); return
        lignes = [(l['id'], l['qte'], l['pu'], l['rem']) for l in self.cart]
        ok, msg, vid = self.db.vendre(lignes, regs, self.client_id, num(self.rg.get()), self.app.user['username'])
        if not ok:
            messagebox.showerror("Vente refusée", msg, parent=self.outer); return
        path = make_ticket(self.db, vid)
        detail = "\n".join(f"  {m} : {fcfa(a)}" for m, a, _ in regs)
        if messagebox.askyesno("Vente enregistrée", f"Total : {fcfa(t)}\n{detail}\n" + (f"Monnaie à rendre : {fcfa(rendu)}\n" if rendu > 0 else "") + "\nOuvrir la facture ?", parent=self.outer):
            open_file(path)
        self.reset(); self.refresh_results()
        self.app.refresh_all(except_=self)


class EncaissementDialog(Dialog):
    """Règlement d'une dette : montant, mode de paiement et référence (obligatoire en mobile money)."""
    def __init__(self, parent, titre, reste, on_ok):
        super().__init__(parent); self.title("Encaisser un règlement"); self.reste, self.on_ok = reste, on_ok
        f = ttk.Frame(self, padding=18); f.pack(fill='both', expand=True)
        ttk.Label(f, text=titre, style='Title.TLabel').pack(anchor='w')
        ttk.Label(f, text=f"Reste dû : {fcfa(reste)}", style='Muted.TLabel').pack(anchor='w', pady=(2, 10))
        g = ttk.Frame(f); g.pack(fill='x'); g.columnconfigure(1, weight=1)
        self.m, self.mode, self.ref = tk.StringVar(value=f"{reste:.0f}"), tk.StringVar(value="Espèces"), tk.StringVar()
        ttk.Label(g, text="Montant reçu").grid(row=0, column=0, sticky='w', pady=4)
        e = ttk.Entry(g, textvariable=self.m, width=16, font=(FONT, 13)); e.grid(row=0, column=1, sticky='w', padx=8)
        ttk.Label(g, text="Mode").grid(row=1, column=0, sticky='w', pady=4)
        ttk.Combobox(g, textvariable=self.mode, values=[m for m in MODES if m != "Crédit client"], state='readonly', width=16).grid(row=1, column=1, sticky='w', padx=8)
        ttk.Label(g, text="Référence").grid(row=2, column=0, sticky='w', pady=4)
        ttk.Entry(g, textvariable=self.ref, width=24).grid(row=2, column=1, sticky='w', padx=8)
        b = ttk.Frame(f); b.pack(fill='x', pady=(14, 0))
        Button(b, text="\u2713  Encaisser", style='Ok.TButton', command=self.ok).pack(side='left')
        Button(b, text="Annuler", command=self.destroy).pack(side='left', padx=8)
        e.bind('<Return>', lambda ev: self.ok()); e.focus_set(); e.selection_range(0, 'end')
        self.transient(parent); self.grab_set()

    def ok(self):
        m = num(self.m.get(), -1)
        if m <= 0 or m > self.reste + 0.001:
            messagebox.showerror("Montant", f"Le montant doit être compris entre 1 et {fcfa(self.reste)}.", parent=self); return
        if self.mode.get() != "Espèces" and not self.ref.get().strip():
            messagebox.showerror("Référence", "Indiquez la référence de la transaction pour ce mode de paiement.", parent=self); return
        mode, ref = self.mode.get(), self.ref.get().strip()
        self.destroy()
        self.on_ok(m, mode, ref)


class TelephoneDialog(Dialog):
    """Assistant « téléphone » en 4 étapes, avec vérification automatique de chaque étape et QR code à scanner."""
    def __init__(self, parent, app):
        super().__init__(parent); self.title("Utiliser l'application sur un téléphone"); self.app = app
        self.geometry("760x720")
        self.sf = ScrollFrame(self); self.sf.pack(fill='both', expand=True)
        self.f = self.sf.inner; self.f.configure(padding=16)
        self.adresse = tk.StringVar()
        self.show_wifi = False
        self.urls = {}
        self.transient(parent); self.grab_set()
        self.build()

    # --- construction (refaite après chaque action pour mettre les statuts à jour)
    def build(self):
        for w in self.f.winfo_children():
            w.destroy()
        app, port = self.app, self.app.net['port']
        serveur = bool(getattr(app, 'server', None))
        ttk.Label(self.f, text="Utiliser l'application sur un téléphone", style='Title.TLabel').pack(anchor='w')
        ttk.Label(self.f, text="Rien à installer sur le téléphone : il ouvre une page web de la boutique. La méthode « par Internet » est la plus simple : elle marche sans Wi-Fi commun, sans pare-feu à régler et même en 4G.",
                  style='Muted.TLabel', wraplength=700).pack(anchor='w', pady=(2, 10))
        # ---- 1. serveur
        c = self.etape(1, "Cet ordinateur doit être le serveur", serveur)
        if serveur:
            ttk.Label(c, text=f"Serveur actif (port {port}). Gardez cet ordinateur allumé avec l'application ouverte.", style='Card.TLabel').pack(anchor='w')
        else:
            ttk.Label(c, text="Le mode « Serveur » n'est pas activé : le téléphone ne pourra rien ouvrir. C'est la cause la plus fréquente d'échec.", style='Card.TLabel',
                      wraplength=640, foreground=THEME['bad']).pack(anchor='w')
            Button(c, text="Activer le mode téléphone (l'application redémarre)", style='Accent.TButton', command=self.activer).pack(anchor='w', pady=(8, 0))
        self.carte_internet(port)
        bt = Button(self.f, text=("Masquer les étapes Wi-Fi" if self.show_wifi else "Autre méthode : par le Wi-Fi de la boutique (sans Internet)..."), command=self.basculer_wifi)
        bt.pack(anchor='w', pady=(12, 0))
        if self.show_wifi:
            self.section_wifi()
        Button(self.f, text="Fermer", command=self.destroy).pack(anchor='w', pady=(10, 0))

    def carte_internet(self, port):
        """Carte « par Internet » : un seul bouton, puis un QR code à scanner."""
        app = self.app
        tun = getattr(app, 'tunnel', None)
        etat = getattr(app, 'tun_etat', None) or {}
        cfg_ = D.get_config()
        fixe = bool(cfg_.get('tunnel_token') and cfg_.get('tunnel_adresse'))
        pret = bool(tun and tun.actif())
        if tun and not tun.actif() and etat.get('phase') == 'ready':       # le tunnel s'est arrêté seul
            etat = app.tun_etat = dict(phase='error', erreur="La connexion Internet s'est interrompue. Réactivez-la.")
        c = self.etape(None, "Par Internet - LE PLUS SIMPLE (recommandé)", True if pret else None)
        if TUN is None:
            ttk.Label(c, text="Le fichier tunnel_boutique.py est absent du dossier de l'application.", style='Card.TLabel', foreground=THEME['bad']).pack(anchor='w'); return
        if not getattr(app, 'server', None):
            ttk.Label(c, text="Activez d'abord le mode téléphone (étape 1).", style='Card.TLabel', foreground=THEME['warn']).pack(anchor='w'); return
        if pret:
            url = tun.url + '/m'
            ttk.Label(c, text="\u2713  Accès par Internet actif et relié à Cloudflare. Scannez ce code avec le téléphone (ou tapez l'adresse) : la page s'ouvre avec le code d'accès déjà rempli.",
                      style='Card.TLabel', foreground=THEME['ok'], font=(FONT, 10, 'bold'), wraplength=640).pack(anchor='w', pady=(0, 6))
            row = ttk.Frame(c, style='Card.TFrame'); row.pack(anchor='w', fill='x')
            cv = tk.Canvas(row, width=210, height=210, bg='white', highlightthickness=0); cv.pack(side='left')
            self.tracer_qr(cv, f"{url}#c={app.net['code']}")
            side = ttk.Frame(row, style='Card.TFrame'); side.pack(side='left', padx=14, anchor='n')
            ttk.Label(side, text=url, style='Card.TLabel', font=(FONT, 10, 'bold'), wraplength=380).pack(anchor='w')
            ttk.Label(side, text=f"Code d'accès : {app.net['code']}", style='Card.TLabel', font=(FONT, 11, 'bold')).pack(anchor='w', pady=4)
            ttk.Label(side, text=("\u2713  ADRESSE FIXE : l'application installée sur le téléphone continuera de fonctionner chaque jour. " if tun.url_fixe else
                                  "Adresse TEMPORAIRE : elle change à chaque activation. Pour INSTALLER l'application sur le téléphone de façon durable, configurez une adresse fixe (option avancée ci-dessous). ") +
                                 "Seules les personnes qui ont l'adresse, le code d'accès ET un identifiant avec son mot de passe peuvent entrer. Gardez l'ordinateur allumé et connecté à Internet.",
                      style='CardMuted.TLabel', wraplength=380, justify='left').pack(anchor='w', pady=4)
            self.live = ttk.Label(side, text="", style='Card.TLabel', font=(FONT, 10, 'bold'), wraplength=380); self.live.pack(anchor='w', pady=2)
            Button(side, text="Vérifier l'adresse maintenant", command=lambda: self.verifier_internet(tun.url)).pack(anchor='w', pady=2)
            Button(side, text="Copier l'adresse", command=lambda: self.copier_texte(url)).pack(anchor='w', pady=2)
            Button(side, text="Imprimer l'affiche QR", command=lambda: self.affiche([url], True)).pack(anchor='w', pady=2)
            Button(side, text="Arrêter l'accès par Internet", style='Danger.TButton', command=self.arreter_internet).pack(anchor='w', pady=2)
            self.suivre()
            return
        phase = etat.get('phase')
        if phase in ('download', 'starting'):
            self.lbl_tun = ttk.Label(c, text="", style='Card.TLabel', font=(FONT, 11, 'bold'), wraplength=640); self.lbl_tun.pack(anchor='w', pady=6)
            self.suivre_tunnel()
            return
        ttk.Label(c, text="Marche partout : en 4G, avec le point d'accès du téléphone, sans box et sans Bluetooth. Il faut seulement qu'Internet fonctionne sur l'ordinateur.\n"
                          "Un petit outil de connexion sécurisée (environ 30 Mo) est téléchargé une seule fois depuis le site officiel de Cloudflare.",
                  style='Card.TLabel', wraplength=640, justify='left').pack(anchor='w')
        if phase == 'error':
            ttk.Label(c, text=etat.get('erreur', ''), style='Card.TLabel', foreground=THEME['bad'], wraplength=640).pack(anchor='w', pady=(6, 0))
        if fixe:
            ttk.Label(c, text=f"Adresse fixe configurée : https://{cfg_['tunnel_adresse'].replace('https://', '')}", style='Card.TLabel', foreground=THEME['ok'], font=(FONT, 10, 'bold')).pack(anchor='w', pady=(6, 0))
        Button(c, text="Activer l'accès par Internet", style='Accent.TButton', command=self.activer_internet).pack(anchor='w', pady=(8, 0))
        self.carte_fixe(c, fixe)

    def carte_fixe(self, c, fixe):
        """Option avancée : adresse FIXE (tunnel nommé Cloudflare). Indispensable pour qu'une application installée sur le téléphone survive aux redémarrages."""
        Button(c, text=("Masquer l'adresse fixe" if getattr(self, 'show_fixe', False) else "Adresse fixe pour l'application installée (avancé)..."), command=self.basculer_fixe).pack(anchor='w', pady=(8, 0))
        if not getattr(self, 'show_fixe', False):
            return
        cfg_ = D.get_config()
        ttk.Label(c, justify='left', wraplength=640, style='Card.TLabel', text=(
            "Avec l'adresse temporaire, l'icône installée sur le téléphone cesse de marcher à la prochaine activation. Pour une adresse qui ne change jamais :\n"
            "1. Créez un compte gratuit sur cloudflare.com et ajoutez un nom de domaine (celui de la boutique, ou un domaine à environ 10 $ par an).\n"
            "2. Zero Trust > Networks > Tunnels > Create a tunnel (Cloudflared) > copiez le JETON (longue suite de caractères).\n"
            "3. Onglet « Public Hostname » : nom (ex. boutique.votredomaine.com), service HTTP, adresse  localhost:8765.\n"
            "4. Collez ci-dessous l'adresse et le jeton, enregistrez, puis activez l'accès par Internet.")).pack(anchor='w', pady=(6, 4))
        self.v_adr, self.v_tok = tk.StringVar(value=cfg_.get('tunnel_adresse', '')), tk.StringVar(value=cfg_.get('tunnel_token', ''))
        g = ttk.Frame(c, style='Card.TFrame'); g.pack(anchor='w', fill='x')
        ttk.Label(g, text="Adresse", style='Card.TLabel').grid(row=0, column=0, sticky='w', pady=2)
        ttk.Entry(g, textvariable=self.v_adr, width=44).grid(row=0, column=1, padx=8)
        ttk.Label(g, text="Jeton", style='Card.TLabel').grid(row=1, column=0, sticky='w', pady=2)
        ttk.Entry(g, textvariable=self.v_tok, width=44, show='\u2022').grid(row=1, column=1, padx=8)
        b = Flow(c); b.pack(anchor='w', fill='x', pady=(6, 0))
        Button(b, text="Enregistrer l'adresse fixe", style='Accent.TButton', command=self.enregistrer_fixe).pack()
        if fixe:
            Button(b, text="Revenir à l'adresse temporaire", command=lambda: self.enregistrer_fixe(vider=True)).pack()

    def basculer_fixe(self):
        self.show_fixe = not getattr(self, 'show_fixe', False)
        self.build()

    def enregistrer_fixe(self, vider=False):
        if self.app.user['role'] != 'admin':
            messagebox.showwarning("Droits", "Seul l'administrateur peut modifier ce réglage.", parent=self); return
        adr, tok = ('', '') if vider else (self.v_adr.get().strip(), self.v_tok.get().strip())
        if not vider and (not adr or len(tok) < 40 or '.' not in adr):
            messagebox.showerror("Adresse fixe", "Indiquez l'adresse (ex. boutique.votredomaine.com) et collez le jeton complet (très long).", parent=self); return
        D.set_config(tunnel_adresse=adr, tunnel_token=tok)
        messagebox.showinfo("Adresse fixe", "Retour à l'adresse temporaire." if vider else "Adresse fixe enregistrée. Activez maintenant l'accès par Internet.", parent=self)
        self.build()

    def activer_internet(self):
        app = self.app
        premiere_fois = not TUN.installe()
        if not messagebox.askyesno("Accès par Internet", ("Un petit outil (environ 30 Mo) va être téléchargé UNE seule fois : cela consomme des données si l'ordinateur utilise "
                                   "le partage de connexion de votre téléphone.\n\n" if premiere_fois else "") +
                                   "L'application va créer une adresse sécurisée que le téléphone pourra ouvrir de n'importe où. Continuer ?", parent=self):
            return
        etat = app.tun_etat = dict(phase='download' if premiere_fois else 'starting', lu=0, total=0, erreur='')
        port = app.net['port']

        def travail():
            try:
                if not TUN.installe():
                    etat['phase'] = 'download'
                    TUN.telecharger(lambda lu, total: etat.update(lu=lu, total=total))
                etat['phase'] = 'starting'
                cfg_ = D.get_config()
                t = TUN.Tunnel(port, token=cfg_.get('tunnel_token', ''), adresse=cfg_.get('tunnel_adresse', ''))
                etat['tunnel'] = t
                t.ouvrir()                                   # attend la vraie liaison avec Cloudflare (évite l'erreur 1033 sur le téléphone)
                app.tunnel = t
                etat['phase'] = 'ready'
            except TUN.ErreurTunnel as e:
                etat.update(phase='error', erreur=str(e))
            except Exception as e:                                      # noqa
                logging.error("Accès par Internet impossible", exc_info=True)
                etat.update(phase='error', erreur=f"Erreur inattendue : {e}")
        threading.Thread(target=travail, daemon=True).start()
        self.build()

    def suivre_tunnel(self):
        """Affiche l'avancement (téléchargement, création de l'adresse) ; reconstruit la fenêtre dès que c'est fini."""
        try:
            if not self.winfo_exists():
                return
        except tk.TclError:
            return
        etat = getattr(self.app, 'tun_etat', None) or {}
        if etat.get('phase') in ('ready', 'error') or not etat:
            self.build(); return
        if etat['phase'] == 'download':
            mo = lambda x: f"{x / 1048576:.0f}"
            self.lbl_tun.config(text=f"Téléchargement de l'outil de connexion sécurisée (une seule fois) : {mo(etat.get('lu', 0))} / {mo(etat['total']) if etat.get('total') else '?'} Mo", foreground=THEME['warn'])
        else:
            t_ = etat.get('tunnel')
            self.lbl_tun.config(text="Création de l'adresse sécurisée et connexion à Cloudflare... (jusqu'à 1 minute)" + (f"\n{t_.essai}" if t_ and t_.essai else ""), foreground=THEME['warn'])
        self._job2 = self.after(700, self.suivre_tunnel)

    def verifier_internet(self, url):
        etat, detail = TUN.verifier(url)
        {'ok': messagebox.showinfo, 'pas_connecte': messagebox.showwarning, 'inconnu': messagebox.showwarning}[etat](
            "Vérification", detail + ("" if etat == 'ok' else "\n\nSi le téléphone affiche « Erreur 1033 », cliquez sur « Arrêter l'accès par Internet » puis réactivez-le."), parent=self)

    def arreter_internet(self):
        t = getattr(self.app, 'tunnel', None)
        if t:
            t.arreter()
        self.app.tunnel, self.app.tun_etat = None, None
        self.build()

    def tracer_qr(self, cv, lien):
        try:
            grille = MB.qr_matrix(lien)
        except Exception:
            cv.delete('all'); cv.create_text(105, 105, text="QR indisponible\n(installez reportlab)\nTapez l'adresse", justify='center'); return
        n, cell = len(grille), 210 // (len(grille) + 4)
        off = (210 - n * cell) // 2
        cv.delete('all')
        for r_, ligne in enumerate(grille):
            for col, noir in enumerate(ligne):
                if noir:
                    cv.create_rectangle(off + col * cell, off + r_ * cell, off + (col + 1) * cell, off + (r_ + 1) * cell, fill='black', outline='black')

    def copier_texte(self, texte):
        self.clipboard_clear(); self.clipboard_append(texte)
        messagebox.showinfo("Téléphone", "Adresse copiée.", parent=self)

    def reglages_hotspot(self):
        try:
            os.startfile("ms-settings:network-mobilehotspot")
        except (AttributeError, OSError):
            messagebox.showinfo("Point d'accès", "Windows : Paramètres > Réseau et Internet > Point d'accès mobile > Activer.", parent=self)

    def suivre(self):
        """Indicateur en direct : le téléphone a-t-il atteint le serveur ? (mis à jour toutes les 2 secondes)"""
        try:
            if not self.winfo_exists():
                return
        except tk.TclError:
            return
        if getattr(self, '_job', None):
            try:
                self.after_cancel(self._job)
            except tk.TclError:
                pass
        if not hasattr(self, 'live') or not self.live.winfo_exists():
            return
        srv = getattr(self.app, 'server', None)
        hits = [h for h in (getattr(srv, 'hits', []) if srv else []) if h['ip'] not in ('127.0.0.1', '::1')]
        if not srv:
            self.live.config(text="Le serveur n'est pas actif (étape 1).", foreground=THEME['bad'])
        elif not hits:
            self.live.config(text="En attente : aucun téléphone n'a encore atteint cet ordinateur.\nOuvrez l'adresse sur le téléphone : cette ligne passera au vert dès qu'il arrive.", foreground=THEME['warn'])
        else:
            h = hits[-1]
            self.live.config(text=f"\u2713  Un téléphone est connecté{' par Internet' if h.get('internet') else ''} : {h['ip']} (il y a {int(time.time() - h['t'])} s).", foreground=THEME['ok'])
        self._job = self.after(2000, self.suivre)

    def section_wifi(self):
        """Méthode locale (sans Internet) : pare-feu, adresse, QR, test. Plus délicate : le téléphone doit être sur le même Wi-Fi."""
        app = self.app
        port = app.net['port']
        # ---- étape 2
        etat = MB.pare_feu_etat(port) if MB else None
        c = self.etape(2, "Autoriser l'application dans le pare-feu Windows", etat)
        if etat is True:
            ttk.Label(c, text="Autorisation déjà faite.", style='Card.TLabel').pack(anchor='w')
        else:
            ttk.Label(c, text=("Sans cette autorisation, Windows bloque le téléphone (deuxième cause d'échec). Cliquez, puis répondez « Oui » à la demande de Windows."
                               if etat is False else "Si Windows affiche « Autoriser l'accès », cliquez sur Autoriser (réseaux privés ET publics)."),
                      style='Card.TLabel', wraplength=640).pack(anchor='w')
            if os.name == 'nt':
                Button(c, text="Autoriser dans le pare-feu", style='Accent.TButton', command=self.pare_feu).pack(anchor='w', pady=(8, 0))
        # ---- étape 3
        ips, ignorees = MB.detecter_reseaux() if MB else ([], [])
        self.urls = {f"{ip}  ({nom})": f"http://{ip}:{port}/m" for ip, nom in ips}
        c = self.etape(3, "Brancher le téléphone sur le même réseau puis scanner", bool(ips))
        ttk.Label(c, text="Connectez le téléphone au MÊME Wi-Fi que cet ordinateur (ou au point d'accès : voir plus bas), puis scannez ce code avec l'appareil photo. "
                          "La page s'ouvre avec le code d'accès déjà rempli : il ne reste que l'identifiant et le mot de passe.", style='Card.TLabel', wraplength=640).pack(anchor='w')
        if ips:
            self.adresse.set(next(iter(self.urls)))
            cb = ttk.Combobox(c, textvariable=self.adresse, values=list(self.urls), state='readonly', width=58); cb.pack(anchor='w', pady=(8, 4))
            cb.bind('<<ComboboxSelected>>', lambda e: self.dessiner())
            row = ttk.Frame(c, style='Card.TFrame'); row.pack(anchor='w', fill='x')
            self.cv = tk.Canvas(row, width=210, height=210, bg='white', highlightthickness=0); self.cv.pack(side='left')
            side = ttk.Frame(row, style='Card.TFrame'); side.pack(side='left', padx=14, anchor='n')
            self.lbl_url = ttk.Label(side, text="", style='Card.TLabel', font=(FONT, 12, 'bold')); self.lbl_url.pack(anchor='w')
            ttk.Label(side, text="Si le QR ne se scanne pas, tapez cette adresse\nen « http:// » (sans le s) dans Chrome ou Safari.", style='CardMuted.TLabel', justify='left').pack(anchor='w', pady=4)
            ttk.Label(side, text=f"Code d'accès : {app.net['code']}", style='Card.TLabel', font=(FONT, 11, 'bold')).pack(anchor='w', pady=4)
            Button(side, text="Copier l'adresse", command=self.copier).pack(anchor='w', pady=2)
            Button(side, text="Imprimer l'affiche QR", command=self.affiche).pack(anchor='w', pady=2)
            self.dessiner()
        else:
            ttk.Label(c, text="Aucune adresse réseau détectée : connectez cet ordinateur au Wi-Fi ou à la box.", style='Card.TLabel', foreground=THEME['bad']).pack(anchor='w', pady=6)
        rangs = [MB._info_ip(ip)[0] for ip, _ in ips]
        self.warn_phone_ap = bool(ips) and min(rangs) == 4
        if self.warn_phone_ap:                                 # le cas qui échoue le plus souvent : le téléphone EST le point d'accès
            ttk.Label(c, text="ATTENTION - Cet ordinateur est connecté au point d'accès (ou au Bluetooth) du téléphone. Dans ce cas, le téléphone qui partage "
                              "sa connexion ne peut en général PAS ouvrir une page de l'ordinateur : c'est une limite d'Android et d'iPhone, pas un défaut de l'application. "
                              "Même si les tests de cet ordinateur sont verts, la ligne de l'étape 4 restera orange.", style='Card.TLabel', foreground=THEME['bad'],
                      wraplength=640, font=(FONT, 10, 'bold')).pack(anchor='w', pady=(8, 0))
            ttk.Label(c, text="Solution sans box : que ce soit l'ORDINATEUR qui crée le Wi-Fi, et le téléphone qui s'y connecte.", style='Card.TLabel', wraplength=640).pack(anchor='w', pady=(4, 0))
            Button(c, text="Ouvrir les réglages « Point d'accès mobile » de Windows", style='Accent.TButton', command=self.reglages_hotspot).pack(anchor='w', pady=(6, 0))
        elif ips and rangs[0] == 0:
            ttk.Label(c, text="\u2713  Point d'accès de l'ordinateur détecté : connectez le téléphone à ce Wi-Fi, puis scannez le code.", style='Card.TLabel',
                      foreground=THEME['ok'], font=(FONT, 10, 'bold'), wraplength=640).pack(anchor='w', pady=(8, 0))
        if ignorees:
            ttk.Label(c, text="Adresses à NE PAS utiliser (réseaux virtuels de Windows : le téléphone ne peut pas les atteindre) :\n" +
                      "\n".join(f"   {ip} - {nom}" for ip, nom in ignorees), style='CardMuted.TLabel', justify='left', wraplength=640).pack(anchor='w', pady=(8, 0))
        if len(ips) > 1:
            ttk.Label(c, text="Plusieurs réseaux sont actifs. Utilisez UN seul chemin : la ligne « Wi-Fi » (point d'accès du téléphone ou box). "
                              "Déconnectez le Bluetooth s'il n'est pas indispensable.", style='Card.TLabel', foreground=THEME['warn'], wraplength=640).pack(anchor='w', pady=(6, 0))
        # ---- étape 4
        c = self.etape(4, "Tester", None)
        self.live = ttk.Label(c, text="", style='Card.TLabel', font=(FONT, 11, 'bold'), wraplength=640); self.live.pack(anchor='w', pady=(0, 6))
        self.res = ttk.Frame(c, style='Card.TFrame'); self.res.pack(anchor='w', fill='x')
        Button(c, text="Tester la connexion maintenant", command=self.tester).pack(anchor='w', pady=(6, 0))
        self.suivre()
        # ---- aide
        aide = card(self.f, 12); aide.pack(fill='x', pady=(10, 0))
        ttk.Label(aide.inner, text="Ça ne marche toujours pas ?", style='CardTitle.TLabel').pack(anchor='w')
        ttk.Label(aide.inner, justify='left', wraplength=660, style='Card.TLabel', text=(
            "RÈGLE D'OR : le téléphone et l'ordinateur doivent être CLIENTS du même Wi-Fi. Celui qui crée le Wi-Fi (le téléphone en point d'accès, ou le Bluetooth\n"
            "du téléphone) ne peut en général pas ouvrir une page d'un appareil connecté à lui. C'est la cause la plus fréquente d'échec.\n\n"
            "A. Vous avez une box ou un routeur Wi-Fi (même sans Internet) - LE PLUS FIABLE\n"
            "   Connectez l'ordinateur ET le téléphone à ce Wi-Fi. Un petit routeur Wi-Fi bon marché suffit, il n'a pas besoin d'Internet.\n\n"
            "B. Pas de box : le point d'accès de l'ORDINATEUR\n"
            "   1. Windows : Paramètres > Réseau et Internet > Point d'accès mobile > Activer (bouton ci-dessus).\n"
            "   2. Téléphone : Paramètres > Wi-Fi > se connecter au réseau de l'ordinateur (nom et mot de passe affichés dans ce même écran Windows).\n"
            "   3. Rouvrez cette fenêtre : l'adresse « point d'accès de l'ordinateur » apparaît (souvent http://192.168.137.1:8765/m). Scannez le QR.\n"
            "   Si Windows refuse de l'activer sans connexion Internet, essayez avec l'ordinateur relié à Internet (câble ou Wi-Fi), ou utilisez A ou C.\n\n"
            "C. Tailscale (gratuit) : marche partout, même en 4G, avec ou sans box\n"
            "   Installez Tailscale sur l'ordinateur ET sur le téléphone, connectez-vous avec le MÊME compte. Rouvrez cette fenêtre :\n"
            "   une adresse « Tailscale » (100.x.x.x) apparaît. Scannez ou tapez-la sur le téléphone. Cette option nécessite Internet sur les deux appareils.\n\n"
            "D. Point d'accès ou Bluetooth DU TÉLÉPHONE : déconseillé (voir la règle d'or). Il ne marche que sur quelques modèles.\n\n"
            "VÉRIFICATIONS\n"
            "   - L'adresse commence par http:// (pas https://) et finit par /m.\n"
            "   - Une adresse 172.x.x.1 (ex. 172.17.192.1) est un réseau virtuel de Windows : elle ne marche jamais depuis un téléphone.\n"
            "   - Pare-feu : étape 2 en vert. Un antivirus avec pare-feu intégré (Kaspersky, Avast, ESET...) peut aussi bloquer : autorisez le port 8765.\n"
            "   - Wi-Fi « invités » : les appareils sont isolés entre eux, utilisez un autre Wi-Fi.\n"
            "   - Désactivez un instant les données mobiles du téléphone pour être sûr qu'il utilise le Wi-Fi.\n"
            "   - Après avoir changé de réseau, rouvrez cette fenêtre : l'adresse a changé.")).pack(anchor='w', pady=6)

    def basculer_wifi(self):
        self.show_wifi = not self.show_wifi
        self.build()

    def etape(self, n, titre, ok):
        c = card(self.f, 12); c.pack(fill='x', pady=4)
        h = ttk.Frame(c.inner, style='Card.TFrame'); h.pack(fill='x', pady=(0, 6))
        sym, col = {True: ("\u2713", THEME['ok']), False: ("\u2715", THEME['bad']), None: ("!", THEME['warn'])}[ok if ok in (True, False) else None]
        ttk.Label(h, text=sym, style='Card.TLabel', foreground=col, font=(FONT, 15, 'bold'), width=2).pack(side='left')
        ttk.Label(h, text=(f"Étape {n} - {titre}" if n else titre), style='CardTitle.TLabel').pack(side='left')
        return c.inner

    # --- actions
    def dessiner(self):
        url = self.urls[self.adresse.get()]
        self.lbl_url.config(text=url)
        self.tracer_qr(self.cv, f"{url}#c={self.app.net['code']}")      # le code est après « # » : il n'est jamais envoyé sur le réseau

    def copier(self):
        self.copier_texte(self.urls[self.adresse.get()])

    def affiche(self, urls=None, internet=False):
        os.makedirs(os.path.join(D.BASE_DIR, 'recus'), exist_ok=True)
        ch = MB.affiche_qr(urls or list(self.urls.values()), os.path.join(D.BASE_DIR, 'recus', 'affiche_telephone.pdf'), internet=internet)
        if ch:
            open_file(ch)
        else:
            messagebox.showwarning("Affiche", "Le module reportlab est nécessaire pour créer l'affiche (pip install reportlab).", parent=self)

    def activer(self):
        if self.app.user['role'] != 'admin':
            messagebox.showwarning("Droits", "Seul l'administrateur peut activer ce mode.", parent=self); return
        if not messagebox.askyesno("Mode téléphone", "L'application va redémarrer en mode Serveur, puis vous vous reconnecterez.\n"
                                   "Les autres ordinateurs pourront aussi s'y connecter avec le code d'accès. Continuer ?", parent=self):
            return
        NET.save_net(dict(mode='serveur', host='', port=self.app.net['port'], code=self.app.net['code'] or NET.new_code()))
        self.destroy()
        self.app.restart()

    def pare_feu(self):
        if MB.pare_feu_autoriser(self.app.net['port']):
            messagebox.showinfo("Pare-feu", "Répondez « Oui » à la fenêtre Windows qui s'ouvre, puis cliquez sur OK ici.", parent=self)
        else:
            messagebox.showwarning("Pare-feu", "La demande n'a pas pu être lancée. Ouvrez l'application « en tant qu'administrateur » ou autorisez-la "
                                   "quand Windows le propose.", parent=self)
        self.build()

    def tester(self):
        for w in self.res.winfo_children():
            w.destroy()
        for nom, ok, detail in MB.tester_acces(self.app.net['port'], [u.split(':')[1][2:] for u in self.urls.values()]):
            ttk.Label(self.res, text=("\u2713  " if ok else "\u2715  ") + f"{nom} : {detail}", style='Card.TLabel',
                      foreground=THEME['ok'] if ok else THEME['bad'], wraplength=640).pack(anchor='w')


class AttentesDialog(Dialog):
    """Liste des ventes mises en attente : reprendre ou supprimer."""
    def __init__(self, parent, vente):
        super().__init__(parent); self.title("Ventes en attente"); self.v = vente
        self.geometry("560x380"); self.transient(parent); self.grab_set()
        ttk.Label(self, text="Ventes en attente aujourd'hui", style='Title.TLabel').pack(anchor='w', padx=12, pady=(12, 4))
        self.t = make_tree(self, [('h', 'Heure'), ('c', 'Client'), ('n', 'Articles'), ('tot', 'Total')], [70, 230, 80, 110], 9)
        self.t.frame.pack(fill='both', expand=True, padx=12)
        self.t.bind('<Double-1>', lambda e: self.ok()); self.t.bind('<Return>', lambda e: self.ok())
        b = ttk.Frame(self); b.pack(fill='x', padx=12, pady=10)
        Button(b, text="Reprendre", style='Accent.TButton', command=self.ok).pack(side='left')
        Button(b, text="Supprimer", command=self.delete).pack(side='left', padx=6)
        Button(b, text="Fermer", command=self.destroy).pack(side='right')
        self.load()

    def load(self):
        clear(self.t)
        for i, h in enumerate(self.v.held):
            self.t.insert('', 'end', iid=str(i), values=(h['heure'], h.get('client') or "Client de passage", len(h['cart']), fcfa(h['total'])))
        if self.t.get_children():
            self.t.selection_set(self.t.get_children()[0])

    def ok(self):
        s = self.t.selection()
        if s:
            self.v.restore_held(int(s[0])); self.destroy()

    def delete(self):
        s = self.t.selection()
        if s and messagebox.askyesno("Supprimer", "Supprimer cette vente en attente ?", parent=self):
            self.v.delete_held(int(s[0])); self.load()


class ClientPicker(Dialog):
    def __init__(self, parent, db, callback):
        super().__init__(parent); self.title("Choisir un client"); self.db, self.cb = db, callback
        self.geometry("560x420"); self.transient(parent); self.grab_set()
        self.q = tk.StringVar(); e = ttk.Entry(self, textvariable=self.q); e.pack(fill='x', padx=8, pady=6); e.focus_set()
        self.q.trace_add('write', lambda *_: self.load())
        self.t = make_tree(self, [('nom', 'Nom'), ('tel', 'Téléphone'), ('solde', 'Solde dû')], [260, 120, 120], 12)
        self.t.frame.pack(fill='both', expand=True, padx=8); self.t.bind('<Double-1>', lambda e: self.ok())
        b = ttk.Frame(self); b.pack(fill='x', padx=8, pady=6)
        Button(b, text="Choisir", style='Accent.TButton', command=self.ok).pack(side='left')
        Button(b, text="Nouveau client...", command=self.new).pack(side='left', padx=6)
        Button(b, text="Client de passage", command=lambda: (self.cb(None), self.destroy())).pack(side='right')
        self.load()

    def load(self):
        clear(self.t)
        for c in self.db.get_clients(self.q.get()):
            self.t.insert('', 'end', iid=str(c['id']), values=(c['nom'], c['telephone'] or '', fcfa(c['solde']).replace(' FCFA', '')))

    def ok(self):
        s = self.t.selection()
        if s:
            self.cb(next(c for c in self.db.get_clients() if c['id'] == int(s[0]))); self.destroy()

    def new(self):
        if ClientDialog(self, self.db).saved:
            self.load()


class ClientDialog(Dialog):
    def __init__(self, parent, db, client=None):
        super().__init__(parent); self.title("Client"); self.db, self.c, self.saved = db, client, False
        self.transient(parent); self.grab_set(); self.resizable(False, False)
        f = ttk.Frame(self, padding=14); f.pack()
        self.v = {k: tk.StringVar(value=str(client[k] if client and client[k] is not None else d)) for k, d in
                  (('nom', ''), ('telephone', ''), ('adresse', ''), ('type', 'particulier'), ('plafond_credit', '0'), ('remise', '0'))}
        for i, (k, lab) in enumerate((('nom', 'Nom *'), ('telephone', 'Téléphone'), ('adresse', 'Adresse'), ('type', 'Type'),
                                      ('plafond_credit', 'Plafond de crédit (FCFA)'), ('remise', 'Remise habituelle %'))):
            ttk.Label(f, text=lab).grid(row=i, column=0, sticky='w', pady=3)
            if k == 'type':
                ttk.Combobox(f, textvariable=self.v[k], values=['particulier', 'ecole', 'entreprise'], state='readonly', width=22).grid(row=i, column=1)
            else:
                ttk.Entry(f, textvariable=self.v[k], width=26).grid(row=i, column=1, pady=3)
        Button(f, text="Enregistrer", style='Accent.TButton', command=self.save).grid(row=7, column=0, columnspan=2, pady=10, sticky='ew')
        self.wait_window()

    def save(self):
        v = {k: x.get() for k, x in self.v.items()}
        ok, msg = self.db.save_client(self.c['id'] if self.c else None, v['nom'], v['telephone'], v['adresse'], v['type'], num(v['plafond_credit']), num(v['remise']))
        if ok:
            self.saved = True; self.destroy()
        else:
            messagebox.showerror("Client", msg, parent=self)


# ============================================================ ACCUEIL (tableau de bord)
class AccueilTab:
    def __init__(self, nb, app):
        self.app, self.db = app, app.db
        self.outer = ScrollFrame(nb); page = self.outer.inner; page.configure(padding=14)
        head = ttk.Frame(page); head.pack(fill='x')
        self.lbl_hello = ttk.Label(head, style='Title.TLabel'); self.lbl_hello.pack(side='left')
        self.lbl_date = ttk.Label(head, style='Muted.TLabel'); self.lbl_date.pack(side='right')
        self.kpis = ttk.Frame(page); self.kpis.pack(fill='x', pady=(12, 4))
        self.acts = Flow(page); self.acts.pack(fill='x', pady=(8, 4))
        mid = ttk.Frame(page); mid.pack(fill='both', expand=True)
        self.left = ttk.Frame(mid); self.left.pack(side='left', fill='both', expand=True)
        self.right = ttk.Frame(mid); self.right.pack(side='left', fill='both', padx=(12, 0))
        # graphique
        self.chart_card = card(self.left); self.chart_card.pack(fill='both', expand=True)
        ttk.Label(self.chart_card.inner, text="Ventes des 7 derniers jours", style='CardTitle.TLabel').pack(anchor='w')
        self.canvas = tk.Canvas(self.chart_card.inner, bg='white', highlightthickness=0, height=190)
        self.canvas.pack(fill='both', expand=True, pady=(6, 0))
        self.canvas.bind('<Configure>', lambda e: self.draw_chart())
        self.top_card = card(self.left); self.top_card.pack(fill='x', pady=(10, 0))
        ttk.Label(self.top_card.inner, text="Articles les plus vendus ce mois-ci", style='CardTitle.TLabel').pack(anchor='w')
        self.top = make_tree(self.top_card.inner, [('a', 'Article'), ('q', 'Qté'), ('ca', 'Montant')], [320, 60, 110], 5)
        self.top.frame.pack(fill='x', pady=(6, 0))
        # alertes
        self.alert_card = card(self.right); self.alert_card.pack(fill='both', expand=True)
        self.alert_card.configure(width=380)
        ttk.Label(self.alert_card.inner, text="À surveiller", style='CardTitle.TLabel').pack(anchor='w')
        self.alerts = ttk.Frame(self.alert_card.inner, style='Card.TFrame'); self.alerts.pack(fill='both', expand=True, pady=(8, 0))
        self.data, self.week = {}, []
        self.load()

    # --- éléments de présentation
    def kpi(self, col, titre, valeur, sous="", couleur=None):
        c = tk.Frame(self.kpis, bg='white', highlightbackground=THEME['line'], highlightthickness=1)
        c.grid(row=0, column=col, sticky='nsew', padx=(0 if col == 0 else 8, 0))
        self.kpis.columnconfigure(col, weight=1, uniform='kpi')
        tk.Frame(c, bg=couleur or THEME['accent'], width=5).pack(side='left', fill='y')
        f = ttk.Frame(c, style='Card.TFrame', padding=(12, 10)); f.pack(side='left', fill='both', expand=True)
        ttk.Label(f, text=titre.upper(), style='CardMuted.TLabel', font=(FONT, 8, 'bold')).pack(anchor='w')
        ttk.Label(f, text=valeur, style='Card.TLabel', font=(FONT, 17, 'bold'), foreground=THEME['head']).pack(anchor='w', pady=(2, 0))
        ttk.Label(f, text=sous or " ", style='CardMuted.TLabel', font=(FONT, 9)).pack(anchor='w')

    def alert(self, niveau, texte, goto=None):
        col = {'bad': THEME['bad'], 'warn': THEME['warn'], 'ok': THEME['ok']}[niveau]
        row = ttk.Frame(self.alerts, style='Card.TFrame'); row.pack(fill='x', pady=3)
        tk.Frame(row, bg=col, width=4, height=34).pack(side='left', fill='y', padx=(0, 8))
        ttk.Label(row, text=texte, style='Card.TLabel', wraplength=270, justify='left').pack(side='left', fill='x', expand=True)
        if goto and goto in self.app.tab_titles:
            Button(row, text="Voir", command=lambda: self.app.goto(goto)).pack(side='right')

    def action(self, texte, fn, style=None):
        Button(self.acts, text=texte, command=fn, style=style).pack()

    # --- données
    def load(self):
        for fr in (self.kpis, self.acts, self.alerts):
            for w in fr.winfo_children():
                w.destroy()
        self.acts._kids = []
        try:
            d = self.db.tableau_de_bord()
        except (NET.NetworkError, NET.SessionError) as e:
            self.alert('bad', f"Réseau indisponible : {e}")
            return
        self.data = d
        u, role = self.app.user, d['role']
        h = datetime.now().hour
        self.lbl_hello.config(text=f"{'Bonjour' if h < 18 else 'Bonsoir'}, {u['full_name'] or u['username']}")
        self.lbl_date.config(text=date_longue())
        if role in ('admin', 'gerant'):
            self.kpi(0, "Ventes du jour", fcfa(d['ca_jour']), f"{d['ventes_jour']} vente(s)", THEME['accent'])
            self.kpi(1, "Marge du jour", fcfa(d['marge_jour']), f"bénéfice net {fcfa(d['benefice_jour'])}", THEME['ok'])
            self.kpi(2, "Chiffre du mois", fcfa(d['ca_mois']), f"marge {d['taux_marge_mois']:.0f} %", THEME['accent'])
            self.kpi(3, "Créances clients", fcfa(d['creances']), f"{d['impayees']} facture(s) impayée(s)", THEME['warn'] if d['creances'] else THEME['ok'])
            self.kpi(4, "Dettes fournisseurs", fcfa(d['dettes_fournisseurs']), "", THEME['warn'] if d['dettes_fournisseurs'] else THEME['ok'])
            self.kpi(5, "Valeur du stock", fcfa(d['valeur_stock']), f"{d['nb_articles']} article(s)", '#7c3aed')
        elif role == 'vendeur':
            self.kpi(0, "Mes ventes du jour", fcfa(d['ca_jour']), f"{d['ventes_jour']} vente(s)", THEME['accent'])
            self.kpi(1, "Espèces encaissées", fcfa(d['especes_jour']), "dans ma caisse aujourd'hui", THEME['ok'])
            self.kpi(2, "Factures impayées", str(d['impayees']), "", THEME['warn'] if d['impayees'] else THEME['ok'])
            self.kpi(3, "Ruptures de stock", str(d['ruptures']), "", THEME['bad'] if d['ruptures'] else THEME['ok'])
        else:
            self.kpi(0, "Ruptures de stock", str(d['ruptures']), "", THEME['bad'] if d['ruptures'] else THEME['ok'])
            self.kpi(1, "Stock bas", str(d['stock_bas']), "à réapprovisionner", THEME['warn'] if d['stock_bas'] else THEME['ok'])
            self.kpi(2, "Articles actifs", str(d['nb_articles']), "", THEME['accent'])
        # raccourcis selon les droits
        if 'Vente' in self.app.tab_titles:
            self.action("\u2713  Nouvelle vente", lambda: self.app.goto('Vente', focus=True), 'Ok.TButton')
        if 'Catalogue' in self.app.tab_titles and role != 'vendeur':
            self.action("+ Nouvel article", lambda: self.app.call_tab('Catalogue', 'new'), 'Accent.TButton')
        if 'Achats' in self.app.tab_titles:
            self.action("Réception de marchandise", lambda: self.app.call_tab('Achats', 'reception'))
        if 'Factures' in self.app.tab_titles:
            self.action("Factures et dettes clients", lambda: self.app.goto('Factures'))
        if 'Caisse' in self.app.tab_titles:
            self.action("Caisse du jour", lambda: self.app.goto('Caisse'))
        # alertes
        n = 0
        if d['ruptures']:
            self.alert('bad', f"{d['ruptures']} article(s) en rupture de stock", 'Stock'); n += 1
        if d['stock_bas']:
            self.alert('warn', f"{d['stock_bas']} article(s) sous le seuil d'alerte", 'Stock'); n += 1
        if role in ('admin', 'gerant', 'vendeur') and d.get('impayees'):
            self.alert('warn', f"{d['impayees']} facture(s) avec un reste à payer", 'Factures'); n += 1
        if role in ('admin', 'gerant', 'vendeur') and d.get('ventes_jour') and not d.get('caisse_cloturee') and h >= 17:
            self.alert('warn', "La caisse du jour n'est pas encore clôturée.", 'Caisse'); n += 1
        n += self.backup_alerts(role)
        if not n:
            self.alert('ok', "Tout est en ordre.")
        self.week = d.get('sept_jours', [])
        clear(self.top)
        for nom, q, ca in d.get('top_mois', []):
            self.top.insert('', 'end', values=(nom, f"{q:g}", fcfa(ca).replace(' FCFA', '')))
        # sans droits sur les rapports : pas de graphique
        for card_, vis in ((self.chart_card, bool(self.week)), (self.top_card, role in ('admin', 'gerant'))):
            card_.pack_forget()
            if vis:
                card_.pack(fill='both' if card_ is self.chart_card else 'x', expand=card_ is self.chart_card, pady=(0 if card_ is self.chart_card else 10, 0))
        self.draw_chart()

    def backup_alerts(self, role):
        if role not in ('admin', 'gerant') or self.app.net['mode'] == 'client':
            return 0
        try:
            e = self.db.etat_sauvegarde()
        except Exception:
            return 0
        n = 0
        if not e['derniere']:
            self.alert('bad', "Aucune sauvegarde n'a encore été faite.", 'Administration'); n += 1
        else:
            age = datetime.now() - datetime.strptime(e['derniere'], '%Y-%m-%d %H:%M:%S')
            if age > timedelta(hours=24):
                self.alert('warn', f"Dernière sauvegarde il y a {age.days} jour(s).", 'Administration'); n += 1
        if not e['externe_configuree']:
            self.alert('warn', "Aucune copie externe : une panne ou un vol de l'ordinateur ferait perdre les données. Branchez une clé USB et choisissez le dossier (Administration > Sauvegardes).", 'Administration'); n += 1
        elif not e['externe_disponible']:
            self.alert('bad', f"Dossier de copie externe introuvable ({e['dossier_externe']}). Clé USB débranchée ?", 'Administration'); n += 1
        return n

    def draw_chart(self):
        c = self.canvas
        c.delete('all')
        if not self.week:
            return
        w, h = max(c.winfo_width(), 300), max(c.winfo_height(), 120)
        bas, haut, marge = h - 26, 22, 14
        vmax = max(v for _, v in self.week) or 1
        pas = (w - 2 * marge) / len(self.week)
        c.create_line(marge, bas, w - marge, bas, fill=THEME['line'])
        for i, (jour, v) in enumerate(self.week):
            x0 = marge + i * pas + pas * 0.18
            x1 = marge + (i + 1) * pas - pas * 0.18
            hh = (bas - haut) * (v / vmax)
            auj = jour == self.data.get('jour')
            c.create_rectangle(x0, bas - max(hh, 1.5), x1, bas, fill=THEME['accent'] if auj else '#a9c1ee', outline='')
            if v:
                c.create_text((x0 + x1) / 2, bas - hh - 8, text=court(v), fill=THEME['head'], font=(FONT, 9, 'bold' if auj else 'normal'))
            d = datetime.strptime(jour, '%Y-%m-%d')
            c.create_text((x0 + x1) / 2, bas + 12, text=f"{JOURS[d.weekday()]} {d.day}", fill=THEME['muted'], font=(FONT, 9, 'bold' if auj else 'normal'))


# ============================================================ onglet ARTICLES
class ArticleDialog(Dialog):
    def __init__(self, parent, db, article=None):
        super().__init__(parent); self.title("Article"); self.db, self.a, self.saved = db, article, False
        self.transient(parent); self.grab_set(); self.resizable(False, False)
        f = ttk.Frame(self, padding=14); f.pack()
        self.cats = {c['nom']: c['id'] for c in db.get_categories()}
        self.fours = {"(aucun)": None, **{x['nom']: x['id'] for x in db.get_fournisseurs()}}
        def g(k, d=''):
            try:
                return str(article[k]) if article and article[k] is not None else d
            except (IndexError, KeyError):
                return d
        self.v = {'designation': tk.StringVar(value=g('designation')), 'categorie': tk.StringVar(value=g('categorie', list(self.cats)[0])),
                  'auteur': tk.StringVar(value=g('auteur')), 'editeur': tk.StringVar(value=g('editeur')), 'isbn': tk.StringVar(value=g('isbn')),
                  'niveau': tk.StringVar(value=g('niveau')), 'code': tk.StringVar(value=g('code')),
                  'prix_achat': tk.StringVar(value=g('prix_achat', '0')), 'prix_vente': tk.StringVar(value=g('prix_vente', '0')),
                  'seuil': tk.StringVar(value=g('seuil_alerte', '5')), 'unite': tk.StringVar(value=g('unite', 'pièce')),
                  'stock': tk.StringVar(value='0'), 'fournisseur': tk.StringVar(value=next((n for n, i in self.fours.items() if article and i == article['fournisseur_id']), '(aucun)')),
                  'emplacement': tk.StringVar(value=g('emplacement'))}
        rows = [('designation', 'Désignation *'), ('categorie', 'Catégorie *'), ('niveau', 'Niveau / classe (livres)'), ('auteur', 'Auteur'),
                ('editeur', 'Éditeur'), ('isbn', 'ISBN / code-barres'), ('code', 'Code interne (auto si vide)'), ('prix_achat', "Prix d'achat"),
                ('prix_vente', 'Prix de vente *'), ('unite', 'Unité'), ('seuil', "Seuil d'alerte stock"), ('fournisseur', 'Fournisseur'),
                ('emplacement', 'Emplacement (rayon)')]
        if not article:
            rows.append(('stock', 'Stock initial'))
        for i, (k, lab) in enumerate(rows):
            ttk.Label(f, text=lab).grid(row=i, column=0, sticky='w', pady=2)
            if k == 'categorie':
                w = ttk.Combobox(f, textvariable=self.v[k], values=list(self.cats), state='readonly', width=38)
            elif k == 'fournisseur':
                w = ttk.Combobox(f, textvariable=self.v[k], values=list(self.fours), state='readonly', width=38)
            elif k == 'unite':
                w = ttk.Combobox(f, textvariable=self.v[k], values=UNITES, width=38)
            else:
                w = ttk.Entry(f, textvariable=self.v[k], width=41)
            w.grid(row=i, column=1, pady=2, padx=6)
        self.lbl = ttk.Label(f, text="", foreground=THEME['warn']); self.lbl.grid(row=len(rows), column=0, columnspan=2)
        for k in ('prix_achat', 'prix_vente'):
            self.v[k].trace_add('write', lambda *_: self.margin())
        self.margin()
        Button(f, text="Enregistrer", style='Accent.TButton', command=self.save).grid(row=len(rows) + 1, column=0, columnspan=2, sticky='ew', pady=8)
        self.wait_window()

    def margin(self):
        pa, pv = num(self.v['prix_achat'].get()), num(self.v['prix_vente'].get())
        if pv > 0 and pa > 0:
            m = (pv - pa) / pv * 100
            self.lbl.config(text=f"Marge : {fcfa(pv - pa)} ({m:.0f} %)" + ("  ⚠ vente sous le coût d'achat" if pv < pa else ""))
        else:
            self.lbl.config(text="")

    def save(self):
        v = {k: x.get() for k, x in self.v.items()}
        avant = dict(self.a) if self.a else None
        ok, msg = self.db.save_article(self.a['id'] if self.a else None, v['designation'], self.cats[v['categorie']], num(v['prix_vente']),
                                       num(v['prix_achat']), v['code'], v['auteur'], v['editeur'], v['isbn'], v['niveau'], v['unite'],
                                       num(v['seuil']), self.fours.get(v['fournisseur']), v['emplacement'], num(v['stock']))
        if ok:
            aid = self.a['id'] if self.a else self.db.last_article_id
            self.saved = True
            recu_auto(self, fiche_article, self.db, aid, avant)          # fiche de l'article créé, ou liste des changements
            self.destroy()
        else:
            messagebox.showerror("Article", msg, parent=self)


class ArticlesTab:
    def __init__(self, nb, app):
        self.app, self.db = app, app.db
        self.outer = ttk.Frame(nb, padding=8)
        bar = ttk.Frame(self.outer); bar.pack(fill='x')
        self.q = tk.StringVar(); self.fam = tk.StringVar(value="Tout"); self.flt = tk.StringVar(value="Tout")
        ttk.Entry(bar, textvariable=self.q, width=34).pack(side='left'); self.q.trace_add('write', lambda *_: self.load())
        ttk.Combobox(bar, textvariable=self.fam, values=["Tout", "Livres", "Fournitures", "Bureau", "Autres"], state='readonly', width=12).pack(side='left', padx=6)
        ttk.Combobox(bar, textvariable=self.flt, values=["Tout", "Stock bas", "Rupture"], state='readonly', width=11).pack(side='left')
        self.fam.trace_add('write', lambda *_: self.load()); self.flt.trace_add('write', lambda *_: self.load())
        acts = Flow(self.outer); acts.pack(fill='x', pady=(8, 0))
        for txt, fn in (("Nouvel article", self.new), ("Modifier", self.edit), ("Archiver", self.archive), ("Importer CSV", self.imp), ("Exporter", self.exp),
                        ("Packs...", lambda: PackManager(self.outer, self.db)), ("Étiquettes", self.labels)):
            Button(acts, text=txt, command=fn).pack()
        self.t = make_tree(self.outer, [('code', 'Code'), ('des', 'Désignation'), ('cat', 'Catégorie'), ('niv', 'Niveau'), ('pa', "Prix achat"),
                                        ('pv', 'Prix vente'), ('st', 'Stock'), ('val', 'Valeur stock')], [90, 300, 170, 70, 85, 85, 60, 100], 18)
        self.t.frame.pack(fill='both', expand=True, pady=6); self.t.bind('<Double-1>', lambda e: self.edit())
        self.lbl = ttk.Label(self.outer); self.lbl.pack(side='bottom', anchor='w')
        self.load()

    def load(self):
        fam = {'Livres': 'livre', 'Fournitures': 'fourniture', 'Bureau': 'bureau', 'Autres': 'autre'}.get(self.fam.get())
        flt = {'Stock bas': 'bas', 'Rupture': 'rupture'}.get(self.flt.get(), '')
        rows = self.db.search_articles(self.q.get(), famille=fam, stock_filtre=flt, limit=2000)
        clear(self.t)
        for a in rows:
            tag = 'rupture' if a['stock'] <= 0 else ('bas' if a['stock'] <= a['seuil_alerte'] else '')
            f = lambda x: f"{x:,.0f}".replace(',', ' ')
            self.t.insert('', 'end', iid=str(a['id']), tags=(tag,), values=(a['code'], a['designation'], a['categorie'], a['niveau'] or '', f(a['prix_achat']), f(a['prix_vente']), f"{a['stock']:g}", f(a['valeur_achat'])))
        vs = self.db.valeur_stock()
        self.lbl.config(text=f"{len(rows)} article(s) affichés   |   Valeur du stock : {fcfa(vs['achat'])} (achat)  /  {fcfa(vs['vente'])} (vente)")

    def sel(self):
        s = self.t.selection()
        return self.db.get_article(int(s[0])) if s else None

    def need_rights(self):
        if self.app.user['role'] not in ('admin', 'gerant', 'magasinier'):
            messagebox.showwarning("Droits", "Réservé au gérant / magasinier.", parent=self.outer); return False
        return True

    def new(self):
        if self.need_rights() and ArticleDialog(self.outer, self.db).saved:
            self.app.refresh_all()

    def edit(self):
        a = self.sel()
        if a and self.need_rights() and ArticleDialog(self.outer, self.db, a).saved:
            self.app.refresh_all()

    def archive(self):
        a = self.sel()
        if a and self.need_rights() and messagebox.askyesno("Archiver", f"Archiver « {a['designation']} » ? (l'historique est conservé)", parent=self.outer):
            self.db.archive_article(a['id']); self.app.refresh_all()

    def labels(self):
        sel = [self.db.get_article(int(i)) for i in self.t.selection()]
        if not sel:
            messagebox.showinfo("Étiquettes", "Sélectionnez un ou plusieurs articles (Ctrl+clic), puis cliquez sur Étiquettes.", parent=self.outer); return
        if not HAS_PDF:
            messagebox.showerror("Étiquettes", "Le module reportlab est nécessaire (pip install reportlab).", parent=self.outer); return
        n = simpledialog.askinteger("Étiquettes", f"{len(sel)} article(s) sélectionné(s).\nNombre d'étiquettes par article :", initialvalue=1, minvalue=1, maxvalue=200, parent=self.outer)
        if n:
            out = os.path.join(D.BASE_DIR, 'recus'); os.makedirs(out, exist_ok=True)
            open_file(make_labels(self.db, [(a, n) for a in sel], os.path.join(out, 'etiquettes.pdf')))

    def exp(self):
        rows = self.db.search_articles(limit=100000)
        export_csv(self.outer, ['code', 'designation', 'categorie', 'niveau', 'auteur', 'isbn', 'prix_achat', 'prix_vente', 'stock'],
                   [(a['code'], a['designation'], a['categorie'], a['niveau'], a['auteur'], a['isbn'], a['prix_achat'], a['prix_vente'], a['stock']) for a in rows], 'catalogue.csv')

    def imp(self):
        if not self.need_rights():
            return
        p = filedialog.askopenfilename(parent=self.outer, filetypes=[('CSV', '*.csv')])
        if not p:
            return
        with open(p, encoding='utf-8-sig', newline='') as f:
            sample = f.read(2048); f.seek(0)
            rd = csv.DictReader(f, delimiter=';' if sample.count(';') >= sample.count(',') else ',')
            rows = [{norm(k).strip(): v for k, v in r.items() if k} for r in rd]
        for r in rows:
            r.setdefault('prix_vente', r.get('prix', r.get('prix de vente', '')))
        ok, errs = self.db.import_articles(rows)
        messagebox.showinfo("Import", f"{ok} article(s) importé(s).\n" + ("\n".join(errs[:12]) if errs else "Aucune erreur.") +
                            "\n\nColonnes attendues : designation; categorie; prix_vente; prix_achat; stock; code; auteur; editeur; isbn; niveau", parent=self.outer)
        self.app.refresh_all()


class PackManager(Dialog):
    """Packs scolaires = liste de fournitures / livres d'une classe, vendus en un clic."""
    def __init__(self, parent, db, select=None):
        super().__init__(parent); self.title("Packs scolaires"); self.db, self.select = db, select
        self.geometry("720x460"); self.transient(parent); self.grab_set()
        self.t = make_tree(self, [('nom', 'Pack'), ('niv', 'Niveau'), ('nb', 'Articles'), ('tot', 'Total (prix actuels)')], [300, 100, 80, 150], 12)
        self.t.frame.pack(fill='both', expand=True, padx=8, pady=8)
        self.det = make_tree(self, [('des', 'Article'), ('q', 'Qté'), ('pu', 'PU'), ('st', 'Stock')], [360, 60, 100, 70], 6)
        self.det.frame.pack(fill='x', padx=8)
        self.t.bind('<<TreeviewSelect>>', lambda e: self.detail())
        bar = ttk.Frame(self); bar.pack(fill='x', padx=8, pady=8)
        if select:
            Button(bar, text="Ajouter au panier", style='Accent.TButton', command=self.choose).pack(side='left')
            self.t.bind('<Double-1>', lambda e: self.choose())
        else:
            Button(bar, text="Nouveau pack", style='Accent.TButton', command=lambda: self.edit(None)).pack(side='left')
            Button(bar, text="Modifier", command=lambda: self.edit(self.sel())).pack(side='left', padx=4)
            Button(bar, text="Supprimer", command=self.delete).pack(side='left')
        Button(bar, text="Fermer", command=self.destroy).pack(side='right')
        self.load()

    def load(self):
        clear(self.t)
        for p in self.db.get_packs():
            self.t.insert('', 'end', iid=str(p['id']), values=(p['nom'], p['niveau'] or '', p['nb_articles'], fcfa(p['total'])))

    def sel(self):
        s = self.t.selection()
        return next((p for p in self.db.get_packs() if p['id'] == int(s[0])), None) if s else None

    def detail(self):
        clear(self.det)
        p = self.sel()
        if p:
            for l in self.db.get_pack_lignes(p['id']):
                self.det.insert('', 'end', tags=('rupture',) if l['stock'] < l['quantite'] else (), values=(l['designation'], f"{l['quantite']:g}", f"{l['prix_vente']:,.0f}".replace(',', ' '), f"{l['stock']:g}"))

    def choose(self):
        p = self.sel()
        if p:
            self.select(p); self.destroy()

    def edit(self, pack):
        PackEditor(self, self.db, pack); self.load()

    def delete(self):
        p = self.sel()
        if p and messagebox.askyesno("Supprimer", f"Supprimer le pack « {p['nom']} » ?", parent=self):
            try:
                self.db.delete_pack(p['id'])
            except PermissionError as e:
                messagebox.showerror("Droits", str(e), parent=self)
            self.load()


class PackEditor(Dialog):
    def __init__(self, parent, db, pack=None):
        super().__init__(parent); self.title("Pack scolaire"); self.db, self.pack_ = db, pack
        self.geometry("640x480"); self.transient(parent); self.grab_set()
        f = ttk.Frame(self, padding=8); f.pack(fill='x')
        self.nom = tk.StringVar(value=pack['nom'] if pack else ''); self.niv = tk.StringVar(value=(pack['niveau'] or '') if pack else '')
        ttk.Label(f, text="Nom du pack").grid(row=0, column=0, sticky='w'); ttk.Entry(f, textvariable=self.nom, width=36).grid(row=0, column=1, padx=6)
        ttk.Label(f, text="Niveau").grid(row=1, column=0, sticky='w'); ttk.Entry(f, textvariable=self.niv, width=14).grid(row=1, column=1, sticky='w', padx=6, pady=4)
        self.lignes = {l['article_id']: [l['designation'], l['quantite']] for l in db.get_pack_lignes(pack['id'])} if pack else {}
        bar = ttk.Frame(self); bar.pack(fill='x', padx=8)
        Button(bar, text="+ Ajouter un article", command=self.add).pack(side='left')
        Button(bar, text="Retirer", command=self.remove).pack(side='left', padx=4)
        self.t = make_tree(self, [('des', 'Article'), ('q', 'Quantité')], [440, 90], 12); self.t.frame.pack(fill='both', expand=True, padx=8, pady=6)
        Button(self, text="Enregistrer le pack", style='Accent.TButton', command=self.save).pack(pady=8)
        self.render(); self.wait_window()

    def render(self):
        clear(self.t)
        for aid, (d, q) in self.lignes.items():
            self.t.insert('', 'end', iid=str(aid), values=(d, f"{q:g}"))

    def add(self):
        a = ArticlePicker(self, self.db, "Article du pack").result
        if a:
            q = simpledialog.askfloat("Quantité", a['designation'], initialvalue=1, minvalue=0.01, parent=self)
            if q:
                self.lignes[a['id']] = [a['designation'], q]; self.render()

    def remove(self):
        for i in self.t.selection():
            self.lignes.pop(int(i), None)
        self.render()

    def save(self):
        try:
            ok, msg = self.db.save_pack(self.pack_['id'] if self.pack_ else None, self.nom.get(), self.niv.get(), [(a, q) for a, (_, q) in self.lignes.items()])
        except PermissionError as e:
            ok, msg = False, str(e)
        if ok:
            self.destroy()
        else:
            messagebox.showerror("Pack", msg, parent=self)


# ============================================================ onglet STOCK
class StockTab:
    def __init__(self, nb, app):
        self.app, self.db = app, app.db
        self.outer = ttk.Frame(nb, padding=8)
        sub = ttk.Notebook(self.outer); sub.pack(fill='both', expand=True)
        # alertes / réappro
        a = ttk.Frame(sub, padding=6); sub.add(a, text="À réapprovisionner")
        self.ta = make_tree(a, [('code', 'Code'), ('des', 'Désignation'), ('four', 'Fournisseur'), ('st', 'Stock'), ('seuil', 'Seuil'), ('cmd', 'Qté à commander'), ('cout', 'Coût estimé')], [90, 280, 160, 60, 60, 110, 110], 14)
        self.ta.frame.pack(fill='both', expand=True)
        b = ttk.Frame(a); b.pack(fill='x', pady=4)
        self.lbl_a = ttk.Label(b); self.lbl_a.pack(side='left')
        Button(b, text="Exporter la liste de commande", command=self.exp_cmd).pack(side='right')
        # mouvements
        m = ttk.Frame(sub, padding=6); sub.add(m, text="Mouvements de stock")
        bar = ttk.Frame(m); bar.pack(fill='x')
        self.mq = tk.StringVar(); ttk.Entry(bar, textvariable=self.mq, width=30).pack(side='left'); self.mq.trace_add('write', lambda *_: self.load_mvt())
        Button(bar, text="Ajuster / perte...", command=self.ajuster).pack(side='right', padx=2)
        Button(bar, text="Inventaire physique...", command=self.inventaire).pack(side='right', padx=2)
        self.tm = make_tree(m, [('date', 'Date'), ('code', 'Code'), ('des', 'Article'), ('type', 'Type'), ('q', 'Qté'), ('apres', 'Stock après'), ('ref', 'Référence'), ('par', 'Par')], [130, 90, 260, 90, 60, 80, 120, 90], 16)
        self.tm.frame.pack(fill='both', expand=True, pady=4)
        # dormants
        d = ttk.Frame(sub, padding=6); sub.add(d, text="Articles dormants (90 j)")
        self.td = make_tree(d, [('code', 'Code'), ('des', 'Désignation'), ('st', 'Stock'), ('imm', 'Capital immobilisé'), ('last', 'Dernière vente')], [90, 320, 70, 130, 110], 16)
        self.td.frame.pack(fill='both', expand=True)
        self.load()

    def load(self):
        clear(self.ta)
        sug = self.db.suggestion_reapprovisionnement(); self.sug = sug
        for s in sug:
            self.ta.insert('', 'end', values=(s['code'], s['designation'], s['fournisseur'] or '', f"{s['stock']:g}", f"{s['seuil']:g}", f"{s['a_commander']:g}", f"{s['cout']:,.0f}".replace(',', ' ')), tags=('rupture' if s['stock'] <= 0 else 'bas',))
        self.lbl_a.config(text=f"{len(sug)} article(s) à commander - coût estimé {fcfa(sum(s['cout'] for s in sug))}")
        self.load_mvt()
        clear(self.td)
        for r in self.db.articles_dormants(90):
            self.td.insert('', 'end', values=(r['code'], r['designation'], f"{r['stock']:g}", f"{r['immobilise']:,.0f}".replace(',', ' '), to_fr(r['derniere_vente']) if r['derniere_vente'] else 'jamais'))

    def load_mvt(self):
        clear(self.tm)
        t = norm(self.mq.get())
        for m in self.db.get_mouvements(limit=800):
            if t and t not in norm(m['designation'] + m['code'] + m['type'] + (m['reference'] or '')):
                continue
            self.tm.insert('', 'end', values=(m['date'][:16], m['code'], m['designation'], m['type'], f"{m['quantite']:+g}", f"{m['stock_apres']:g}", m['reference'] or '', m['par']))

    def exp_cmd(self):
        export_csv(self.outer, ['code', 'designation', 'fournisseur', 'stock', 'seuil', 'a_commander', 'cout'],
                   [(s['code'], s['designation'], s['fournisseur'], s['stock'], s['seuil'], s['a_commander'], s['cout']) for s in self.sug], 'commande.csv')

    def pick(self, title):
        return ArticlePicker(self.outer, self.db, title).result

    def ajuster(self):
        a = self.pick("Article à ajuster")
        if not a:
            return
        d = simpledialog.askfloat("Ajustement", f"{a['designation']} - stock actuel : {a['stock']:g}\nQuantité (+ entrée / - sortie) :", parent=self.outer)
        if not d:
            return
        motif = ask_reason(self.outer, "Motif", "Motif obligatoire (casse, vol, don, erreur...) :")
        if motif is None:
            return
        try:
            ok, msg = self.db.ajuster_stock(a['id'], d, 'perte' if d < 0 else 'ajustement', motif)
        except PermissionError as e:
            ok, msg = False, str(e)
        (messagebox.showinfo if ok else messagebox.showerror)("Stock", msg, parent=self.outer)
        if ok:
            recu_auto(self.outer, bon_ajustement, self.db, self.db.last_mouvement_id)
        self.app.refresh_all()

    def inventaire(self):
        InventaireDialog(self.outer, self.db); self.app.refresh_all()


class ArticlePicker(Dialog):
    def __init__(self, parent, db, title, only_in_stock=False):
        super().__init__(parent); self.title(title); self.db, self.result = db, None
        self.geometry("620x440"); self.transient(parent); self.grab_set()
        self.q = tk.StringVar(); e = ttk.Entry(self, textvariable=self.q, font=(FONT, 11)); e.pack(fill='x', padx=8, pady=6); e.focus_set()
        self.q.trace_add('write', lambda *_: self.load())
        self.t = make_tree(self, [('code', 'Code'), ('des', 'Désignation'), ('pa', 'Prix achat'), ('st', 'Stock')], [90, 340, 90, 60], 14)
        self.t.frame.pack(fill='both', expand=True, padx=8, pady=4); self.t.bind('<Double-1>', lambda e: self.ok())
        e.bind('<Return>', lambda e: self.ok())
        Button(self, text="Choisir", style='Accent.TButton', command=self.ok).pack(pady=6)
        self.load(); self.wait_window()

    def load(self):
        clear(self.t)
        for a in self.db.search_articles(self.q.get(), limit=80):
            self.t.insert('', 'end', iid=str(a['id']), values=(a['code'], a['designation'], f"{a['prix_achat']:,.0f}".replace(',', ' '), f"{a['stock']:g}"))

    def ok(self):
        s = self.t.selection() or (self.t.get_children()[:1] if len(self.t.get_children()) == 1 else ())
        if s:
            self.result = self.db.get_article(int(s[0])); self.destroy()


class InventaireDialog(Dialog):
    """Saisie des quantités comptées ; seuls les écarts génèrent un mouvement."""
    def __init__(self, parent, db):
        super().__init__(parent); self.title("Inventaire physique"); self.db = db
        self.geometry("760x520"); self.transient(parent); self.grab_set()
        ttk.Label(self, text="Double-clic sur une ligne pour saisir la quantité comptée. Seuls les articles saisis sont corrigés.", padding=8).pack(anchor='w')
        bar = ttk.Frame(self); bar.pack(fill='x', padx=8)
        self.q = tk.StringVar(); ttk.Entry(bar, textvariable=self.q, width=30).pack(side='left'); self.q.trace_add('write', lambda *_: self.load())
        self.t = make_tree(self, [('code', 'Code'), ('des', 'Désignation'), ('st', 'Stock théorique'), ('cpt', 'Qté comptée'), ('ec', 'Écart')], [90, 330, 100, 100, 80], 16)
        self.t.frame.pack(fill='both', expand=True, padx=8, pady=6); self.t.bind('<Double-1>', lambda e: self.saisir())
        self.counts, self.theo = {}, {}
        Button(self, text="Valider l'inventaire", style='Accent.TButton', command=self.valider).pack(pady=6)
        self.load(); self.wait_window()

    def load(self):
        clear(self.t)
        for a in self.db.search_articles(self.q.get(), limit=400):
            self.theo[a['id']] = a['stock']
            c = self.counts.get(a['id'])
            self.t.insert('', 'end', iid=str(a['id']), values=(a['code'], a['designation'], f"{a['stock']:g}", '' if c is None else f"{c:g}", '' if c is None else f"{c - a['stock']:+g}"))

    def saisir(self):
        s = self.t.selection()
        if not s:
            return
        aid = int(s[0])
        v = simpledialog.askfloat("Comptage", "Quantité réellement comptée :", minvalue=0, parent=self)
        if v is not None:
            self.counts[aid] = v; self.load()

    def valider(self):
        if not self.counts:
            return
        if not messagebox.askyesno("Inventaire", f"Corriger le stock de {len(self.counts)} article(s) selon vos comptages ?", parent=self):
            return
        try:
            ec = self.db.inventaire(self.counts)
        except PermissionError as e:
            messagebox.showerror("Droits", str(e), parent=self); return
        messagebox.showinfo("Inventaire", f"{len(ec)} écart(s) corrigé(s).\nValeur des écarts : {fcfa(sum(e[5] for e in ec))}", parent=self)
        self.destroy()


# ============================================================ onglet ACHATS / FOURNISSEURS
class AchatsTab:
    def __init__(self, nb, app):
        self.app, self.db = app, app.db
        self.outer = ttk.Frame(nb, padding=8)
        sub = ttk.Notebook(self.outer); sub.pack(fill='both', expand=True)
        f = ttk.Frame(sub, padding=6); sub.add(f, text="Fournisseurs")
        bar = ttk.Frame(f); bar.pack(fill='x')
        Button(bar, text="Nouveau fournisseur", command=lambda: self.edit_f(None)).pack(side='left')
        Button(bar, text="Modifier", command=lambda: self.edit_f(self.sel_f())).pack(side='left', padx=4)
        self.tf = make_tree(f, [('nom', 'Fournisseur'), ('contact', 'Contact'), ('tel', 'Téléphone'), ('dette', 'Dette envers lui')], [240, 160, 120, 130], 14)
        self.tf.frame.pack(fill='both', expand=True, pady=4)
        a = ttk.Frame(sub, padding=6); sub.add(a, text="Réceptions / achats")
        bar = ttk.Frame(a); bar.pack(fill='x')
        Button(bar, text="Nouvelle réception de marchandises", style='Accent.TButton', command=self.reception).pack(side='left')
        Button(bar, text="Payer une facture fournisseur", command=self.payer).pack(side='left', padx=6)
        self.ta = make_tree(a, [('bon', 'Bon'), ('date', 'Date'), ('four', 'Fournisseur'), ('ref', 'Réf. fournisseur'), ('total', 'Total'), ('paye', 'Payé'), ('reste', 'Reste')], [110, 90, 200, 130, 100, 100, 100], 14)
        self.ta.frame.pack(fill='both', expand=True, pady=4)
        self.load()

    def load(self):
        clear(self.tf)
        for x in self.db.get_fournisseurs():
            self.tf.insert('', 'end', iid=str(x['id']), values=(x['nom'], x['contact'] or '', x['telephone'] or '', fcfa(x['dette']).replace(' FCFA', '')), tags=('dette',) if x['dette'] > 0 else ())
        clear(self.ta)
        for x in self.db.get_achats():
            self.ta.insert('', 'end', iid=str(x['id']), values=(x['bon_no'], to_fr(x['date']), x['fournisseur'] or '-', x['reference_fournisseur'] or '', f"{x['total']:,.0f}".replace(',', ' '), f"{x['paye']:,.0f}".replace(',', ' '), f"{x['total'] - x['paye']:,.0f}".replace(',', ' ')),
                           tags=('dette',) if x['total'] - x['paye'] > 0.001 else ())

    def sel_f(self):
        s = self.tf.selection()
        return next((f for f in self.db.get_fournisseurs() if f['id'] == int(s[0])), None) if s else None

    def edit_f(self, f):
        nom = simpledialog.askstring("Fournisseur", "Nom :", initialvalue=f['nom'] if f else '', parent=self.outer)
        if not nom:
            return
        tel = simpledialog.askstring("Fournisseur", "Téléphone :", initialvalue=(f['telephone'] or '') if f else '', parent=self.outer) or ''
        try:
            ok, msg = self.db.save_fournisseur(f['id'] if f else None, nom, f['contact'] if f else '', tel, f['adresse'] if f else '')
        except PermissionError as e:
            ok, msg = False, str(e)
        if not ok:
            messagebox.showerror("Fournisseur", msg, parent=self.outer)
        self.app.refresh_all()

    def reception(self):
        ReceptionDialog(self.outer, self.db); self.app.refresh_all()

    def payer(self):
        s = self.ta.selection()
        if not s:
            messagebox.showinfo("Paiement", "Sélectionnez une facture.", parent=self.outer); return
        v = simpledialog.askfloat("Paiement fournisseur", "Montant payé (FCFA) :", minvalue=1, parent=self.outer)
        if v:
            try:
                ok, msg = self.db.payer_fournisseur(int(s[0]), v)
            except PermissionError as e:
                ok, msg = False, str(e)
            (messagebox.showinfo if ok else messagebox.showerror)("Paiement", msg, parent=self.outer)
            self.app.refresh_all()


class ReceptionDialog(Dialog):
    def __init__(self, parent, db):
        super().__init__(parent); self.title("Réception de marchandises"); self.db = db; self.lignes = []
        self.geometry("760x560"); self.transient(parent); self.grab_set()
        top = ttk.Frame(self, padding=8); top.pack(fill='x')
        self.fours = {x['nom']: x['id'] for x in db.get_fournisseurs()}
        ttk.Label(top, text="Fournisseur").grid(row=0, column=0); self.fv = tk.StringVar()
        ttk.Combobox(top, textvariable=self.fv, values=list(self.fours), state='readonly', width=28).grid(row=0, column=1, padx=6)
        ttk.Label(top, text="N° bon de livraison").grid(row=0, column=2); self.ref = tk.StringVar(); ttk.Entry(top, textvariable=self.ref, width=18).grid(row=0, column=3, padx=6)
        Button(self, text="+ Ajouter un article", command=self.add).pack(anchor='w', padx=8)
        self.t = make_tree(self, [('des', 'Article'), ('q', 'Qté'), ('pa', "Prix d'achat"), ('tot', 'Total')], [360, 70, 110, 110], 12)
        self.t.frame.pack(fill='both', expand=True, padx=8, pady=6)
        bot = ttk.Frame(self, padding=8); bot.pack(fill='x')
        self.lbl = ttk.Label(bot, text="Total : 0 FCFA", font=(FONT, 12, 'bold')); self.lbl.pack(side='left')
        ttk.Label(bot, text="  Payé maintenant :").pack(side='left'); self.paye = tk.StringVar(value="0"); ttk.Entry(bot, textvariable=self.paye, width=12).pack(side='left')
        Button(bot, text="Enregistrer la réception", style='Accent.TButton', command=self.ok).pack(side='right')
        self.wait_window()

    def add(self):
        a = ArticlePicker(self, self.db, "Article reçu").result
        if not a:
            return
        q = simpledialog.askfloat("Quantité reçue", a['designation'], minvalue=0.01, parent=self)
        pa = simpledialog.askfloat("Prix d'achat unitaire", f"{a['designation']}\nDernier prix : {a['prix_achat']:,.0f}", initialvalue=a['prix_achat'], minvalue=0, parent=self)
        if q and pa is not None:
            self.lignes.append((a['id'], a['designation'], q, pa)); self.render()

    def render(self):
        clear(self.t)
        for _, d, q, pa in self.lignes:
            self.t.insert('', 'end', values=(d, f"{q:g}", f"{pa:,.0f}".replace(',', ' '), f"{round(q * pa):,}".replace(',', ' ')))
        self.lbl.config(text=f"Total : {fcfa(sum(round(q * pa) for _, _, q, pa in self.lignes))}")

    def ok(self):
        ok, msg, achat_id = self.db.acheter(self.fours.get(self.fv.get()), [(a, q, pa) for a, _, q, pa in self.lignes], num(self.paye.get()), self.ref.get())
        if ok:
            messagebox.showinfo("Réception", msg, parent=self)
            recu_auto(self, bon_reception, self.db, achat_id)
            self.destroy()
        else:
            messagebox.showerror("Réception", msg, parent=self)


# ============================================================ onglet VENTES (historique, retours)
class HistoriqueTab:
    def __init__(self, nb, app):
        self.app, self.db = app, app.db
        self.outer = ttk.Frame(nb, padding=8)
        bar = ttk.Frame(self.outer); bar.pack(fill='x')
        self.d1 = tk.StringVar(value=date.today().isoformat()); self.d2 = tk.StringVar(value=date.today().isoformat())
        self.q = tk.StringVar(); self.imp = tk.BooleanVar(value=False)
        ttk.Label(bar, text="Du").pack(side='left'); ttk.Entry(bar, textvariable=self.d1, width=11).pack(side='left', padx=3)
        ttk.Label(bar, text="au").pack(side='left'); ttk.Entry(bar, textvariable=self.d2, width=11).pack(side='left', padx=3)
        ttk.Entry(bar, textvariable=self.q, width=20).pack(side='left', padx=6)
        ttk.Checkbutton(bar, text="Impayées seulement", variable=self.imp, command=self.load).pack(side='left')
        Button(bar, text="Afficher", command=self.load).pack(side='left', padx=4)
        acts = Flow(self.outer); acts.pack(fill='x', pady=(8, 0))
        for txt, fn in (("Encaisser le reste", self.encaisser), ("Ticket 80 mm", self.reprint), ("Facture A4", self.reprint_a4),
                        ("Retour client", self.retour), ("Annuler la vente", self.annuler)):
            Button(acts, text=txt, command=fn).pack()
        self.t = make_tree(self.outer, [('fac', 'Facture'), ('date', 'Date'), ('client', 'Client'), ('vend', 'Vendeur'), ('total', 'Total'), ('paye', 'Payé'), ('reste', 'Reste')], [120, 120, 200, 100, 100, 100, 100], 14)
        self.t.frame.pack(fill='both', expand=True, pady=6); self.t.bind('<<TreeviewSelect>>', lambda e: self.detail())
        self.d = make_tree(self.outer, [('des', 'Article'), ('q', 'Qté'), ('pu', 'PU'), ('tot', 'Total'), ('ret', 'Retournée')], [340, 60, 90, 100, 80], 5)
        self.d.frame.pack(fill='x')
        self.lbl = ttk.Label(self.outer); self.lbl.pack(side='bottom', anchor='w')
        self.load()

    def load(self):
        clear(self.t)
        rows = self.db.get_ventes(self.d1.get(), self.d2.get(), term=self.q.get(), impayees=self.imp.get())
        tot = 0
        for v in rows:
            reste = 0 if v['annule'] else v['total'] - v['paye']
            if not v['annule']:
                tot += v['total']
            self.t.insert('', 'end', iid=str(v['id']), tags=('annule',) if v['annule'] else (('dette',) if reste > 0.001 else ()),
                          values=(v['facture_no'] + (' (ANNULÉE)' if v['annule'] else ''), v['date'][:16], v['client'] or 'Passage', v['vendeur'], f"{v['total']:,.0f}".replace(',', ' '), f"{v['paye']:,.0f}".replace(',', ' '), f"{reste:,.0f}".replace(',', ' ')))
        self.lbl.config(text=f"{len(rows)} facture(s) - total {fcfa(tot)}")

    def vid(self):
        s = self.t.selection()
        return int(s[0]) if s else None

    def detail(self):
        clear(self.d)
        if self.vid():
            for l in self.db.get_vente_lignes(self.vid()):
                self.d.insert('', 'end', iid=str(l['id']), values=(l['designation'], f"{l['quantite']:g}", f"{l['prix_unitaire']:,.0f}".replace(',', ' '), f"{l['total']:,.0f}".replace(',', ' '), f"{l['qte_retournee']:g}"))

    def reprint(self):
        if self.vid():
            open_file(make_ticket(self.db, self.vid(), duplicata=True, format='ticket'))

    def filter_client(self, nom, impayees=False):
        """Appelé depuis l'onglet Clients : affiche toutes les factures (ou les impayées) d'un client."""
        self.d1.set("2000-01-01"); self.d2.set(date.today().isoformat())
        self.q.set(nom); self.imp.set(impayees); self.load()

    def reprint_a4(self):
        if self.vid():
            open_file(make_ticket(self.db, self.vid(), duplicata=True, format='a4'))

    def annuler(self):
        if not self.vid():
            return
        m = ask_reason(self.outer, "Annuler la vente", "Motif de l'annulation (obligatoire) :")
        if m is None:
            return
        try:
            ok, msg = self.db.annuler_vente(self.vid(), m)
        except PermissionError as e:
            ok, msg = False, str(e)
        (messagebox.showinfo if ok else messagebox.showerror)("Annulation", msg, parent=self.outer); self.app.refresh_all()

    def retour(self):
        s = self.d.selection()
        if not s:
            messagebox.showinfo("Retour", "Sélectionnez la ligne de l'article retourné (tableau du bas).", parent=self.outer); return
        q = simpledialog.askfloat("Retour", "Quantité retournée :", minvalue=0.01, parent=self.outer)
        if not q:
            return
        m = ask_reason(self.outer, "Retour", "Motif du retour :")
        if m is None:
            return
        rs = messagebox.askyesno("Retour", "L'article est-il en bon état (le remettre en stock) ?", parent=self.outer)
        try:
            ok, msg, _ = self.db.retourner(int(s[0]), q, m, rs)
        except PermissionError as e:
            ok, msg = False, str(e)
        (messagebox.showinfo if ok else messagebox.showerror)("Retour", msg, parent=self.outer); self.app.refresh_all()

    def encaisser(self):
        if not self.vid():
            return
        v = self.db.get_vente(self.vid())
        reste = v['total'] - v['paye']
        if v['annule'] or reste <= 0.001:
            messagebox.showinfo("Encaissement", "Rien à encaisser sur cette facture.", parent=self.outer); return
        def valider(m, mode, ref):
            try:
                ok, msg, rid = self.db.encaisser_credit(v['id'], m, mode, ref, self.app.user['username'])
            except PermissionError as e:
                ok, msg, rid = False, str(e), None
            (messagebox.showinfo if ok else messagebox.showerror)("Encaissement", msg, parent=self.outer)
            if ok:
                recu_auto(self.outer, recu_reglement, self.db, rid)       # reçu remis au client
            self.app.refresh_all()
        EncaissementDialog(self.outer, f"Facture {v['facture_no']}", reste, valider)


# ============================================================ onglet CLIENTS
class ClientsTab:
    def __init__(self, nb, app):
        self.app, self.db = app, app.db
        self.outer = ttk.Frame(nb, padding=8)
        bar = ttk.Frame(self.outer); bar.pack(fill='x')
        self.q = tk.StringVar(); ttk.Entry(bar, textvariable=self.q, width=30).pack(side='left'); self.q.trace_add('write', lambda *_: self.load())
        acts = Flow(self.outer); acts.pack(fill='x', pady=(8, 0))
        Button(acts, text="Nouveau client", command=lambda: self.edit(None)).pack()
        Button(acts, text="Modifier", command=lambda: self.edit(self.sel())).pack()
        Button(acts, text="Factures du client", command=lambda: self.voir(False)).pack()
        Button(acts, text="Dettes à encaisser", command=lambda: self.voir(True)).pack()
        self.t = make_tree(self.outer, [('nom', 'Nom'), ('type', 'Type'), ('tel', 'Téléphone'), ('plaf', 'Plafond crédit'), ('solde', 'Solde dû')], [260, 100, 120, 120, 120], 18)
        self.t.frame.pack(fill='both', expand=True, pady=6)
        self.lbl = ttk.Label(self.outer); self.lbl.pack(side='bottom', anchor='w')
        self.load()

    def voir(self, impayees):
        sel = self.t.selection()
        if not sel:
            messagebox.showinfo("Clients", "Sélectionnez d'abord un client dans la liste.", parent=self.outer); return
        nom = self.t.item(sel[0])['values'][0]
        if 'Factures' not in self.app.tab_titles:
            return
        self.app.goto('Factures')
        self.app.tab_by_title['Factures'].filter_client(str(nom), impayees)

    def load(self):
        clear(self.t); tot = 0
        for c in self.db.get_clients(self.q.get()):
            tot += c['solde']
            self.t.insert('', 'end', iid=str(c['id']), tags=('dette',) if c['solde'] > 0 else (), values=(c['nom'], c['type'], c['telephone'] or '', f"{c['plafond_credit']:,.0f}".replace(',', ' '), f"{c['solde']:,.0f}".replace(',', ' ')))
        self.lbl.config(text=f"Créances clients totales : {fcfa(tot)}")

    def sel(self):
        s = self.t.selection()
        return next((c for c in self.db.get_clients() if c['id'] == int(s[0])), None) if s else None

    def edit(self, c):
        if ClientDialog(self.outer, self.db, c).saved:
            self.app.refresh_all()


# ============================================================ onglet CAISSE
class CaisseTab:
    def __init__(self, nb, app):
        self.app, self.db = app, app.db
        self.outer = ScrollFrame(nb); page = self.outer.inner; page.configure(padding=12)
        ttk.Label(page, text="Caisse du jour", style='Title.TLabel').pack(anchor='w')
        self.txt = tk.Text(page, height=14, width=80, font=('Consolas' if sys.platform.startswith('win') else 'Courier', 11), bg='white', relief='flat')
        self.txt.pack(anchor='w', pady=8)
        bar = Flow(page); bar.pack(fill='x')
        Button(bar, text="Enregistrer une dépense", command=self.depense).pack(side='left', padx=2)
        Button(bar, text="Clôturer ma caisse", style='Accent.TButton', command=self.cloturer).pack(side='left', padx=2)
        Button(bar, text="Rouvrir (gérant)", command=self.rouvrir).pack(side='left', padx=2)
        self.load()

    def load(self):
        j = date.today().isoformat(); u = self.app.user['username']
        d = self.db.caisse_du_jour(j, u)
        L = [f"Caissier : {self.app.user['full_name']}    Jour : {to_fr(j)}", "-" * 62]
        for m, (n, s) in d['par_mode'].items():
            L.append(f"  {m:<20} {n:>3} règlement(s) {fcfa(s):>20}")
        L += ["-" * 62, f"  Espèces encaissées       {fcfa(d['especes']):>30}", f"  - Dépenses               {fcfa(d['depenses']):>30}",
              f"  - Remboursements clients {fcfa(d['remboursements']):>30}", f"  = ESPÈCES ATTENDUES EN CAISSE {fcfa(d['attendu']):>25}",
              f"  Autres modes (hors tiroir) {fcfa(d['autres']):>28}"]
        if d['cloture']:
            L += ["", f"  CAISSE CLÔTURÉE - compté {fcfa(d['cloture']['compte'])}, écart {fcfa(d['cloture']['ecart'])}"]
        self.txt.config(state='normal'); self.txt.delete('1.0', 'end'); self.txt.insert('1.0', '\n'.join(L)); self.txt.config(state='disabled')

    def depense(self):
        lib = simpledialog.askstring("Dépense", "Libellé (transport, électricité, salaire...) :", parent=self.outer)
        if not lib:
            return
        m = simpledialog.askfloat("Dépense", "Montant (FCFA) :", minvalue=1, parent=self.outer)
        if m:
            try:
                ok, msg = self.db.add_depense(lib, m)
            except PermissionError as e:
                ok, msg = False, str(e)
            (messagebox.showinfo if ok else messagebox.showerror)("Dépense", msg, parent=self.outer)
            if ok:
                recu_auto(self.outer, bon_depense, self.db, self.db.last_depense_id)
            self.load()

    def cloturer(self):
        j, u = date.today().isoformat(), self.app.user['username']
        d = self.db.caisse_du_jour(j, u)
        c = simpledialog.askfloat("Clôture", f"Espèces attendues : {fcfa(d['attendu'])}\n\nMontant réellement compté dans le tiroir :", minvalue=0, parent=self.outer)
        if c is None:
            return
        note = ""
        if abs(c - d['attendu']) > 0.001:
            note = ask_reason(self.outer, "Écart de caisse", f"Écart de {fcfa(c - d['attendu'])}. Explication :") or ""
        ok, msg, _ = self.db.cloturer(j, u, c, note)
        (messagebox.showinfo if ok else messagebox.showerror)("Clôture", msg, parent=self.outer); self.load()

    def rouvrir(self):
        cl = self.db.cloture_active(date.today().isoformat(), self.app.user['username'])
        if not cl:
            messagebox.showinfo("Caisse", "Aucune clôture active.", parent=self.outer); return
        m = ask_reason(self.outer, "Rouvrir", "Motif :")
        if m is not None:
            try:
                ok, msg = self.db.rouvrir_cloture(cl['id'], m)
            except PermissionError as e:
                ok, msg = False, str(e)
            (messagebox.showinfo if ok else messagebox.showerror)("Caisse", msg, parent=self.outer); self.load()


# ============================================================ onglet RAPPORTS
class RapportsTab:
    def __init__(self, nb, app):
        self.app, self.db = app, app.db
        self.outer = ttk.Frame(nb, padding=8)
        bar = ttk.Frame(self.outer); bar.pack(fill='x')
        self.d1 = tk.StringVar(value=date.today().replace(day=1).isoformat()); self.d2 = tk.StringVar(value=date.today().isoformat())
        ttk.Label(bar, text="Du").pack(side='left'); ttk.Entry(bar, textvariable=self.d1, width=11).pack(side='left', padx=3)
        ttk.Label(bar, text="au").pack(side='left'); ttk.Entry(bar, textvariable=self.d2, width=11).pack(side='left', padx=3)
        Button(bar, text="Aujourd'hui", command=lambda: self.preset(0)).pack(side='left', padx=2)
        Button(bar, text="7 jours", command=lambda: self.preset(6)).pack(side='left', padx=2)
        Button(bar, text="Ce mois", command=lambda: self.preset(None)).pack(side='left', padx=2)
        Button(bar, text="Actualiser", style='Accent.TButton', command=self.load).pack(side='left', padx=6)
        self.kpi = ttk.Frame(self.outer); self.kpi.pack(fill='x', pady=8)
        self.labels = {}
        for i, (k, lab) in enumerate((('ca', "Chiffre d'affaires"), ('marge', 'Marge brute'), ('benefice', 'Bénéfice estimé'), ('encaisse', 'Encaissé'), ('creances', 'Créances clients'), ('dettes_fournisseurs', 'Dettes fournisseurs'))):
            f = ttk.Frame(self.kpi, padding=8, relief='groove'); f.grid(row=0, column=i, padx=4, sticky='nsew'); self.kpi.columnconfigure(i, weight=1)
            ttk.Label(f, text=lab).pack(); l = ttk.Label(f, text="-", font=(FONT, 12, 'bold'), foreground=THEME['accent']); l.pack(); self.labels[k] = l
        sub = ttk.Notebook(self.outer); sub.pack(fill='both', expand=True)
        self.tabs = {}
        for key, title, cols, w in (('fam', "Par catégorie", [('c', 'Catégorie'), ('q', 'Qté'), ('ca', 'CA'), ('m', 'Marge')], [300, 80, 120, 120]),
                                    ('top', "Meilleures ventes", [('c', 'Code'), ('d', 'Article'), ('q', 'Qté'), ('ca', 'CA')], [90, 330, 80, 120]),
                                    ('jour', "Par jour", [('j', 'Jour'), ('n', 'Factures'), ('ca', 'CA')], [140, 90, 140]),
                                    ('vend', "Par vendeur", [('v', 'Vendeur'), ('n', 'Factures'), ('ca', 'CA')], [200, 90, 140]),
                                    ('cli', "Meilleurs clients", [('c', 'Client'), ('n', 'Factures'), ('ca', 'CA')], [260, 90, 140]),
                                    ('inv', "Inventaire", [('c', 'Code'), ('d', 'Article'), ('cat', 'Catégorie'), ('deb', 'Début'), ('ach', 'Achats'), ('ret', 'Retours'),
                                                           ('ven', 'Ventes'), ('aj', 'Pertes/Ajust.'), ('fin', 'Fin'), ('va', 'Valeur achat'), ('vv', 'Valeur vente')],
                                     [90, 260, 150, 60, 60, 60, 60, 90, 60, 110, 110])):
            f = ttk.Frame(sub, padding=4); sub.add(f, text=title)
            t = make_tree(f, cols, w, 12); t.frame.pack(fill='both', expand=True); self.tabs[key] = t
        Button(self.outer, text="Exporter l'onglet affiché (CSV)", command=lambda: self.export(sub)).pack(anchor='e', pady=4)
        # onglet Inventaire : options, impression PDF, totaux
        fi = self.tabs['inv'].frame.master
        self.inv_tout = tk.BooleanVar(value=False)
        top = ttk.Frame(fi); top.pack(side='top', fill='x', before=self.tabs['inv'].frame, pady=(0, 4))
        Button(top, text="Imprimer l'inventaire (PDF)", style='Accent.TButton', command=self.inventaire_pdf).pack(side='left')
        ttk.Checkbutton(top, text="Inclure les articles sans stock ni mouvement", variable=self.inv_tout, command=self.load).pack(side='left', padx=12)
        self.lbl_inv = ttk.Label(fi, style='Muted.TLabel'); self.lbl_inv.pack(side='bottom', anchor='w', pady=(4, 0))
        self.sub = sub; self.load()

    def inventaire_pdf(self):
        d1, d2 = self.d1.get().strip(), self.d2.get().strip()
        if not periode_ok(self.outer, d1, d2):
            return
        if recu_auto(self.outer, rapport_inventaire, self.db, d1, d2, self.inv_tout.get(), forcer_ouverture=True):
            messagebox.showinfo("Inventaire", "L'inventaire est créé en PDF et son édition est inscrite au journal d'audit.", parent=self.outer)

    def preset(self, days):
        self.d2.set(date.today().isoformat())
        self.d1.set((date.today().replace(day=1) if days is None else date.today() - timedelta(days=days)).isoformat()); self.load()

    def load(self):
        d1, d2 = self.d1.get().strip(), self.d2.get().strip()
        if not periode_ok(self.outer, d1, d2):
            return
        r = self.db.resume_periode(d1, d2)
        for k, l in self.labels.items():
            l.config(text=fcfa(r[k]))
        f = lambda x: f"{x:,.0f}".replace(',', ' ')
        for t in self.tabs.values():
            clear(t)
        for x in self.db.ventes_par_famille(d1, d2):
            self.tabs['fam'].insert('', 'end', values=(x['categorie'], f"{x['qte']:g}", f(x['ca']), f(x['marge'])))
        for x in self.db.top_articles(d1, d2, 30):
            self.tabs['top'].insert('', 'end', values=(x['code'], x['designation'], f"{x['qte']:g}", f(x['ca'])))
        for x in self.db.ventes_par_jour(d1, d2):
            self.tabs['jour'].insert('', 'end', values=(to_fr(x['jour']), x['nb'], f(x['ca'])))
        for x in self.db.ventes_par_vendeur(d1, d2):
            self.tabs['vend'].insert('', 'end', values=(x['vendeur'], x['nb'], f(x['ca'])))
        for x in self.db.top_clients(d1, d2):
            self.tabs['cli'].insert('', 'end', values=(x['nom'], x['nb'], f(x['ca'])))
        inv = self.db.inventaire_periode(d1, d2, self.inv_tout.get())
        q = lambda v: f"{v:,.0f}".replace(',', ' ') if abs(v - round(v)) < 1e-9 else f"{v:g}"
        for l in inv['lignes']:
            self.tabs['inv'].insert('', 'end', values=(l['code'], l['designation'], l['categorie'], q(l['debut']), q(l['achats']), q(l['retours']), q(l['ventes']),
                                                       (f"{l['ajust']:+g}" if l['ajust'] else "0"), q(l['fin']), f(l['valeur_achat']), f(l['valeur_vente'])),
                                    tags=('rupture',) if l['fin'] <= 0 else ())
        t = inv['totaux']
        self.lbl_inv.config(text=f"{t['nb']} article(s)  |  stock fin : {q(t['fin'])} unité(s)  |  valeur à l'achat : {fcfa(t['valeur_achat'])}  |  "
                                 f"valeur à la vente : {fcfa(t['valeur_vente'])}  |  marge potentielle : {fcfa(t['marge_potentielle'])}")

    def export(self, sub):
        key = list(self.tabs)[sub.index(sub.select())]
        t = self.tabs[key]
        export_csv(self.outer, [t.heading(c)['text'].replace(' \u25b2', '').replace(' \u25bc', '') for c in t['columns']], [t.item(i)['values'] for i in t.get_children()], f"rapport_{key}.csv")


# ============================================================ ADMIN (utilisateurs, audit, paramètres)
class AdminTab:
    def __init__(self, nb, app):
        self.app, self.db = app, app.db
        self.outer = ttk.Frame(nb, padding=8)
        sub = ttk.Notebook(self.outer); sub.pack(fill='both', expand=True)
        u = ttk.Frame(sub, padding=6); sub.add(u, text="Utilisateurs")
        bar = ttk.Frame(u); bar.pack(fill='x')
        bar = Flow(u); bar.pack(fill='x')
        Button(bar, text="Nouvel utilisateur", command=self.add_user).pack()
        Button(bar, text="Modifier le rôle", command=self.edit_user).pack()
        Button(bar, text="Réinitialiser le mot de passe", command=self.reset_pw).pack()
        Button(bar, text="Supprimer", command=self.del_user).pack()
        self.tu = make_tree(u, [('u', 'Identifiant'), ('n', 'Nom'), ('r', 'Rôle'), ('l', 'Dernière connexion')], [150, 220, 160, 150], 10); self.tu.frame.pack(fill='both', expand=True, pady=4)
        sp = ScrollFrame(sub); sub.add(sp, text="Paramètres"); p = sp.inner; p.configure(padding=14)
        self.cv = {}
        cfg = D.get_config()
        champs = (('boutique', 'Nom de la boutique'), ('adresse', 'Adresse'), ('telephone', 'Téléphone'), ('niu', "NIU (n° d'identifiant unique)"),
                  ('rccm', 'RCCM'), ('tva_taux', 'TVA % (0 = aucune)'), ('seuil_remise_vendeur', 'Remise max. autorisée à un vendeur %'),
                  ('message_recu', 'Message en bas de la facture'), ('mentions', 'Mention légale en bas de la facture'),
                  ('format_recu', 'Format de facture par défaut'), ('ouvrir_recus', 'Ouvrir automatiquement les reçus (oui / non)'), ('logo', 'Logo (image PNG ou JPG)'))
        for i, (k, lab) in enumerate(champs):
            ttk.Label(p, text=lab).grid(row=i, column=0, sticky='w', pady=4)
            self.cv[k] = tk.StringVar(value=str(cfg.get(k, '')))
            if k in ('format_recu', 'ouvrir_recus'):
                ttk.Combobox(p, textvariable=self.cv[k], values=['ticket', 'a4'] if k == 'format_recu' else ['oui', 'non'], state='readonly', width=12).grid(row=i, column=1, padx=8, sticky='w')
                if k == 'format_recu':
                    ttk.Label(p, text="ticket = rouleau 80 mm   |   a4 = facture pleine page", style='Muted.TLabel').grid(row=i, column=2, sticky='w')
            elif k == 'logo':
                ttk.Entry(p, textvariable=self.cv[k], width=50, state='readonly').grid(row=i, column=1, padx=8)
                lf = Flow(p); lf.grid(row=i, column=2, sticky='w')
                Button(lf, text="Choisir un logo...", style='Accent.TButton', command=self.pick_logo).pack()
                Button(lf, text="Retirer", command=self.remove_logo).pack()
                self.logo_prev = ttk.Label(p); self.logo_prev.grid(row=i, column=3, padx=8)
                self.maj_logo()
            else:
                ttk.Entry(p, textvariable=self.cv[k], width=50).grid(row=i, column=1, padx=8)
        Button(p, text="Enregistrer", style='Accent.TButton', command=self.save_cfg).grid(row=len(champs), column=0, pady=12, sticky='w')
        Button(p, text="Aperçu de l'en-tête des factures", command=self.apercu_logo).grid(row=len(champs), column=1, pady=12, sticky='w', padx=8)
        s_ = ttk.Frame(sub, padding=10); sub.add(s_, text="Sauvegardes")
        self.build_backups(s_)
        sn = ScrollFrame(sub); sub.add(sn, text="Réseau"); n = sn.inner; n.configure(padding=10)
        self.build_net(n)
        c = ttk.Frame(sub, padding=6); sub.add(c, text="Catégories")
        cb = Flow(c); cb.pack(fill='x')
        Button(cb, text="Nouvelle catégorie", command=self.add_cat).pack()
        Button(cb, text="Modifier", command=self.edit_cat).pack()
        self.tc = make_tree(c, [('n', 'Catégorie'), ('f', 'Famille')], [360, 120], 14); self.tc.frame.pack(fill='both', expand=True, pady=4)
        a = ttk.Frame(sub, padding=6); sub.add(a, text="Journal d'audit")
        bar = ttk.Frame(a); bar.pack(fill='x')
        self.aq = tk.StringVar(); ttk.Entry(bar, textvariable=self.aq, width=22).pack(side='left'); self.aq.trace_add('write', lambda *_: self.load_audit())
        self.ad1 = tk.StringVar(value=date.today().replace(day=1).isoformat()); self.ad2 = tk.StringVar(value=date.today().isoformat())
        ttk.Label(bar, text="   Du").pack(side='left'); ttk.Entry(bar, textvariable=self.ad1, width=11).pack(side='left', padx=3)
        ttk.Label(bar, text="au").pack(side='left'); ttk.Entry(bar, textvariable=self.ad2, width=11).pack(side='left', padx=3)
        Button(bar, text="Imprimer le journal (PDF)", style='Accent.TButton', command=self.audit_pdf).pack(side='left', padx=6)
        Button(bar, text="Vérifier l'intégrité", command=lambda: messagebox.showinfo("Journal", self.db.verify_audit()[1], parent=self.outer)).pack(side='right')
        self.ta = make_tree(a, [('d', 'Date'), ('u', 'Utilisateur'), ('a', 'Action'), ('x', 'Détails')], [130, 110, 160, 520], 16); self.ta.frame.pack(fill='both', expand=True, pady=4)
        self.load()

    def load(self):
        clear(self.tu)
        for x in self.db.get_users():
            self.tu.insert('', 'end', iid=str(x['id']), values=(x['username'], x['full_name'], ROLES.get(x['role'], x['role']), x['last_login'] or ''))
        clear(self.tc)
        for c in self.db.get_categories():
            self.tc.insert('', 'end', iid=str(c['id']), values=(c['nom'], c['famille']))
        self.load_audit()
        self.load_backups()

    def audit_pdf(self):
        d1, d2 = self.ad1.get().strip(), self.ad2.get().strip()
        if not periode_ok(self.outer, d1, d2):
            return
        recu_auto(self.outer, journal_audit_pdf, self.db, d1, d2, self.aq.get().strip(), forcer_ouverture=True)
        self.load_audit()                                         # l'impression est elle-même inscrite au journal

    def load_audit(self):
        clear(self.ta)
        for r in self.db.get_audit(self.aq.get(), limit=400):
            self.ta.insert('', 'end', values=(r['date'], r['utilisateur'], r['action'], r['details']))

    def add_user(self):
        if self.app.user['role'] != 'admin':
            messagebox.showwarning("Droits", "Réservé à l'administrateur.", parent=self.outer); return
        u = simpledialog.askstring("Utilisateur", "Identifiant :", parent=self.outer)
        n = u and simpledialog.askstring("Utilisateur", "Nom complet :", parent=self.outer)
        r = n and simpledialog.askstring("Rôle", "Rôle (admin / gerant / vendeur / magasinier) :", initialvalue='vendeur', parent=self.outer)
        pw = r and simpledialog.askstring("Mot de passe", "Mot de passe temporaire (8 caractères min., lettres + chiffres) :", show='*', parent=self.outer)
        if not pw:
            return
        msg = D.check_password(pw)
        if msg or r not in ROLES:
            messagebox.showerror("Utilisateur", msg or "Rôle inconnu.", parent=self.outer); return
        try:
            self.db.add_user(u, pw, r, n, must_change=True)
        except Exception as e:
            messagebox.showerror("Utilisateur", f"Création impossible : {e}", parent=self.outer)
        self.load()

    def del_user(self):
        s = self.tu.selection()
        if s and self.app.user['role'] == 'admin' and messagebox.askyesno("Supprimer", "Supprimer cet utilisateur ?", parent=self.outer):
            ok, msg = self.db.delete_user(int(s[0]))
            if not ok:
                messagebox.showerror("Utilisateur", msg, parent=self.outer)
            self.load()

    def edit_user(self):
        sel = self.tu.selection()
        if not sel:
            messagebox.showinfo("Utilisateur", "Sélectionnez d'abord un utilisateur.", parent=self.outer); return
        v = self.tu.item(sel[0])['values']
        n = simpledialog.askstring("Utilisateur", "Nom complet :", initialvalue=v[1], parent=self.outer)
        r = n and simpledialog.askstring("Rôle", "Rôle (admin / gerant / vendeur / magasinier) :", initialvalue=next((k for k, x in ROLES.items() if x == v[2]), 'vendeur'), parent=self.outer)
        if r:
            ok, msg = self.db.update_user(int(sel[0]), r.strip(), n)
            (messagebox.showinfo if ok else messagebox.showerror)("Utilisateur", msg, parent=self.outer)
            self.load()

    def reset_pw(self):
        sel = self.tu.selection()
        if not sel:
            messagebox.showinfo("Mot de passe", "Sélectionnez d'abord un utilisateur.", parent=self.outer); return
        pw = simpledialog.askstring("Mot de passe", "Nouveau mot de passe temporaire (8 caractères min., lettres + chiffres) :", show='*', parent=self.outer)
        if pw:
            ok, msg = self.db.reset_password(int(sel[0]), pw)
            (messagebox.showinfo if ok else messagebox.showerror)("Mot de passe", msg, parent=self.outer)

    def edit_cat(self):
        sel = self.tc.selection()
        if not sel:
            messagebox.showinfo("Catégorie", "Sélectionnez d'abord une catégorie.", parent=self.outer); return
        v = self.tc.item(sel[0])['values']
        n = simpledialog.askstring("Catégorie", "Nom de la catégorie :", initialvalue=v[0], parent=self.outer)
        f = n and simpledialog.askstring("Famille", "Famille (livre / fourniture / bureau / autre) :", initialvalue=v[1], parent=self.outer)
        if f:
            ok, msg = self.db.save_categorie(int(sel[0]), n, f if f in ('livre', 'fourniture', 'bureau', 'autre') else 'autre')
            if not ok:
                messagebox.showerror("Catégorie", msg, parent=self.outer)
            self.load()

    def add_cat(self):
        n = simpledialog.askstring("Catégorie", "Nom de la catégorie :", parent=self.outer)
        f = n and simpledialog.askstring("Famille", "Famille (livre / fourniture / bureau / autre) :", initialvalue='autre', parent=self.outer)
        if f:
            try:
                ok, msg = self.db.save_categorie(None, n, f if f in ('livre', 'fourniture', 'bureau', 'autre') else 'autre')
            except PermissionError as e:
                ok, msg = False, str(e)
            if not ok:
                messagebox.showerror("Catégorie", msg, parent=self.outer)
            self.load()

    def build_net(self, f):
        cfg = NET.load_net(); self.nv = {k: tk.StringVar(value=str(cfg[k])) for k in ('mode', 'host', 'port', 'code')}
        ttk.Label(f, text="Plusieurs ordinateurs peuvent partager le même stock et les mêmes ventes.\n"
                          "Un ordinateur est le SERVEUR (il garde la base), les autres sont des CLIENTS (même Wi-Fi / même box).", wraplength=720).grid(row=0, column=0, columnspan=3, sticky='w', pady=(0, 10))
        for i, (v, lab) in enumerate((('autonome', "Poste unique (sans réseau)"), ('serveur', "Serveur (cet ordinateur garde la base)"), ('client', "Client (se connecte à un serveur)"))):
            ttk.Radiobutton(f, text=lab, variable=self.nv['mode'], value=v).grid(row=1 + i, column=0, columnspan=3, sticky='w')
        ttk.Label(f, text="Adresse du serveur (client)").grid(row=4, column=0, sticky='w', pady=4); ttk.Entry(f, textvariable=self.nv['host'], width=22).grid(row=4, column=1, sticky='w')
        ttk.Label(f, text="Port").grid(row=5, column=0, sticky='w'); ttk.Entry(f, textvariable=self.nv['port'], width=8).grid(row=5, column=1, sticky='w')
        ttk.Label(f, text="Code d'accès").grid(row=6, column=0, sticky='w', pady=4); ttk.Entry(f, textvariable=self.nv['code'], width=14).grid(row=6, column=1, sticky='w')
        Button(f, text="Nouveau code", command=lambda: self.nv['code'].set(NET.new_code())).grid(row=6, column=2, sticky='w')
        ips = ', '.join(NET.lan_ips()) or "(non détectée)"
        ttk.Label(f, text=f"Adresse(s) de cet ordinateur sur le réseau : {ips}", foreground=THEME['accent']).grid(row=7, column=0, columnspan=3, sticky='w', pady=6)
        Button(f, text="Enregistrer (redémarrez l'application)", style='Accent.TButton', command=self.save_net).grid(row=8, column=0, columnspan=2, sticky='w', pady=6)
        self.lbl_srv = ttk.Label(f, text=""); self.lbl_srv.grid(row=9, column=0, columnspan=3, sticky='w', pady=6)
        Button(f, text="Actualiser les postes connectés", command=self.show_clients).grid(row=10, column=0, sticky='w')
        Button(f, text="Utiliser avec un téléphone...", style='Accent.TButton', command=self.phone).grid(row=10, column=1, columnspan=2, sticky='w', padx=8)
        self.show_clients()

    def phone(self):
        self.app.phone_help()

    def show_clients(self):
        srv = getattr(self.app, 'server', None)
        if srv:
            cl = srv.clients(minutes=30)
            self.lbl_srv.config(text=f"Serveur actif sur le port {srv.port} - {len(cl)} poste(s) connecté(s) :\n" + "\n".join(f"   {u} ({r}) - {ip}" for u, _, r, ip, _ in cl))
        else:
            self.lbl_srv.config(text={'client': "Ce poste est connecté à un serveur.", 'serveur': "Serveur non démarré (voir le message au lancement)."}.get(self.app.net['mode'], "Mode poste unique."))

    def save_net(self):
        if self.app.user['role'] != 'admin':
            messagebox.showwarning("Droits", "Réservé à l'administrateur.", parent=self.outer); return
        v = {k: x.get().strip() for k, x in self.nv.items()}
        if v['mode'] == 'serveur' and not v['code']:
            v['code'] = NET.new_code(); self.nv['code'].set(v['code'])
        if v['mode'] == 'client' and (not v['host'] or not v['code']):
            messagebox.showerror("Réseau", "Indiquez l'adresse du serveur et le code d'accès.", parent=self.outer); return
        NET.save_net(v)
        messagebox.showinfo("Réseau", "Enregistré. Fermez puis relancez l'application pour appliquer le nouveau mode.", parent=self.outer)

    def save_cfg(self):
        if self.app.net['mode'] == 'client':
            messagebox.showinfo("Paramètres", "Ces paramètres se modifient sur le poste serveur.", parent=self.outer); return
        v = {k: x.get() for k, x in self.cv.items()}
        v['tva_taux'], v['seuil_remise_vendeur'] = num(v['tva_taux']), num(v['seuil_remise_vendeur'])
        if not (0 <= v['tva_taux'] <= 100) or not (0 <= v['seuil_remise_vendeur'] <= 100):
            messagebox.showerror("Paramètres", "La TVA et la remise maximale doivent être comprises entre 0 et 100.", parent=self.outer); return
        if not v['boutique'].strip():
            messagebox.showerror("Paramètres", "Le nom de la boutique est obligatoire.", parent=self.outer); return
        if v['logo'].strip() and not os.path.isfile(v['logo'].strip()):
            messagebox.showerror("Paramètres", "Le fichier du logo est introuvable.", parent=self.outer); return
        D.set_config(**v); messagebox.showinfo("Paramètres", "Enregistré.", parent=self.outer)

    def pick_logo(self):
        """Le logo choisi est COPIÉ dans le dossier de l'application (PNG 600 px) : il reste affiché même si le fichier d'origine est déplacé."""
        f = filedialog.askopenfilename(parent=self.outer, title="Logo de l'entreprise", filetypes=[("Images", "*.png *.jpg *.jpeg *.bmp *.gif *.webp")])
        if not f:
            return
        try:
            dest = installer_logo(f)
        except (ValueError, TypeError) as e:
            messagebox.showerror("Logo", str(e) if installer_logo else "Le fichier facture_boutique.py est absent.", parent=self.outer); return
        D.set_config(logo=dest)
        self.cv['logo'].set(dest); self.maj_logo()
        messagebox.showinfo("Logo", "Logo enregistré : il s'affiche en haut de toutes les factures et reçus, dans l'application du téléphone et sur l'icône installée.\n"
                            "Cliquez sur « Aperçu de l'en-tête des factures » pour le vérifier.", parent=self.outer)

    def remove_logo(self):
        lg = (self.cv['logo'].get() or '').strip()
        if lg and messagebox.askyesno("Logo", "Retirer le logo des factures ?", parent=self.outer):
            D.set_config(logo="")
            self.cv['logo'].set("")
            try:
                if os.path.basename(lg) == 'logo.png':
                    os.remove(lg)
            except OSError:
                pass
            self.maj_logo()

    def maj_logo(self):
        """Miniature du logo à côté du champ."""
        lg = (self.cv['logo'].get() or '').strip()
        self._prev_img = None
        try:
            if lg and os.path.isfile(lg):
                img = tk.PhotoImage(file=lg)
                f_ = max(1, -(-img.height() // 56))
                self._prev_img = img.subsample(f_, f_)
        except Exception:
            self._prev_img = None
        self.logo_prev.config(image=self._prev_img or '', text="" if self._prev_img else "(aucun logo)")

    def apercu_logo(self):
        recu_auto(self.outer, apercu_entete, forcer_ouverture=True)

    # --- sauvegardes : copie externe + restauration
    def build_backups(self, f):
        self.is_client = self.app.net['mode'] == 'client'
        self._files = []
        self.lbl_bk = ttk.Label(f, wraplength=900, justify='left'); self.lbl_bk.pack(anchor='w')
        c1 = card(f, 10); c1.pack(fill='x', pady=8)
        i = c1.inner
        ttk.Label(i, text="Copie externe (clé USB, disque, dossier synchronisé)", style='CardTitle.TLabel').grid(row=0, column=0, columnspan=4, sticky='w')
        self.ext = tk.StringVar(value=D.get_config().get('backup_externe', ''))
        ttk.Entry(i, textvariable=self.ext, width=60).grid(row=1, column=0, columnspan=2, sticky='w', pady=6)
        Button(i, text="Choisir le dossier...", command=self.pick_ext).grid(row=1, column=2, padx=6)
        ttk.Label(i, text="Sauvegarde automatique toutes les", style='Card.TLabel').grid(row=2, column=0, sticky='w')
        self.auto = tk.StringVar(value=f"{D.get_config().get('sauvegarde_auto_min', 20):g}")
        ae = ttk.Frame(i, style='Card.TFrame'); ae.grid(row=2, column=1, sticky='w')
        ttk.Entry(ae, textvariable=self.auto, width=5).pack(side='left'); ttk.Label(ae, text=" minutes (si des données ont changé)", style='CardMuted.TLabel').pack(side='left')
        Button(i, text="Enregistrer", style='Accent.TButton', command=self.save_backup_cfg).grid(row=2, column=2, padx=6)
        bar = ttk.Frame(f); bar.pack(fill='x')
        Button(bar, text="Sauvegarder maintenant", style='Accent.TButton', command=self.backup).pack(side='left')
        Button(bar, text="Restaurer la sélection", command=self.restore_selected).pack(side='left', padx=6)
        Button(bar, text="Restaurer depuis un fichier...", command=self.restore_file).pack(side='left')
        self.tb = make_tree(f, [('d', 'Date'), ('l', 'Emplacement'), ('n', 'Fichier'), ('t', 'Taille')], [150, 100, 360, 90], 10)
        self.tb.frame.pack(fill='both', expand=True, pady=8)
        ttk.Label(f, text="Restaurer remplace toutes les données actuelles. L'état actuel est d'abord mis de côté (fichier « avant_restauration »), "
                          "et seul l'administrateur peut le faire.", style='Muted.TLabel', wraplength=900).pack(anchor='w')
        if self.is_client:
            self.lbl_bk.config(text="Les sauvegardes se gèrent sur le poste serveur (celui qui garde la base).")

    def load_backups(self):
        clear(self.tb)
        if self.is_client:
            return
        try:
            e = self.db.etat_sauvegarde()
            fichiers = self.db.liste_sauvegardes()
        except (PermissionError, NET.NetworkError, NET.SessionError):
            return
        parts = [f"Dernière sauvegarde : {e['derniere'] or 'aucune'}"]
        parts.append("Copie externe : " + ("non configurée" if not e['externe_configuree'] else
                                           (f"OK ({e['derniere_externe'] or 'aucune copie encore'})" if e['externe_disponible'] else "DOSSIER INTROUVABLE")))
        self.lbl_bk.config(text="   |   ".join(parts))
        for i, x in enumerate(fichiers):
            self.tb.insert('', 'end', iid=str(i), values=(x['date'], x['lieu'], x['nom'], f"{x['taille'] / 1024:,.0f} Ko".replace(',', ' ')))
        self._files = fichiers

    def pick_ext(self):
        d = filedialog.askdirectory(parent=self.outer, title="Dossier de copie externe")
        if d:
            self.ext.set(d)

    def save_backup_cfg(self):
        if self.is_client:
            messagebox.showinfo("Sauvegardes", "Ces réglages se font sur le poste serveur.", parent=self.outer); return
        minutes = num(self.auto.get(), 20)
        if minutes < 5:
            messagebox.showwarning("Sauvegardes", "L'intervalle minimum est de 5 minutes.", parent=self.outer); return
        ext = self.ext.get().strip()
        if ext and not os.path.isdir(ext):
            messagebox.showwarning("Sauvegardes", "Ce dossier n'existe pas : branchez la clé USB puis choisissez-le avec le bouton.", parent=self.outer); return
        D.set_config(backup_externe=ext, sauvegarde_auto_min=minutes)
        self.app.schedule_backup()
        messagebox.showinfo("Sauvegardes", "Réglages enregistrés.", parent=self.outer); self.load_backups()

    def backup(self):
        if self.is_client:
            messagebox.showinfo("Sauvegarde", "Les sauvegardes se font sur le poste serveur.", parent=self.outer); return
        try:
            path = self.db.backup()
        except Exception as e:
            messagebox.showerror("Sauvegarde", f"La sauvegarde a échoué : {e}", parent=self.outer); return
        ok, msg = getattr(self.db, 'ext_etat', (None, ''))
        fonction = messagebox.showinfo if ok is not False else messagebox.showwarning
        fonction("Sauvegarde", f"Sauvegarde créée :\n{path}\n\n{msg}", parent=self.outer)
        self.load_backups()

    def restore_selected(self):
        s = self.tb.selection()
        if not s:
            messagebox.showinfo("Restauration", "Sélectionnez d'abord une sauvegarde dans la liste.", parent=self.outer); return
        self.do_restore(self._files[int(s[0])]['chemin'])

    def restore_file(self):
        f = filedialog.askopenfilename(parent=self.outer, filetypes=[("Sauvegarde", "*.db")])
        if f:
            self.do_restore(f)

    def do_restore(self, chemin):
        if self.is_client or self.app.user['role'] != 'admin':
            messagebox.showwarning("Restauration", "Réservée à l'administrateur, sur le poste serveur.", parent=self.outer); return
        ok, msg = self.db.verifier_sauvegarde(chemin)
        if not ok:
            messagebox.showerror("Restauration", msg, parent=self.outer); return
        mot = simpledialog.askstring("Restauration", f"{msg}\n\nFichier : {os.path.basename(chemin)}\n\nToutes les données actuelles seront REMPLACÉES par celles de ce fichier.\n"
                                     "Les autres postes doivent être fermés.\n\nTapez RESTAURER pour confirmer :", parent=self.outer)
        if (mot or '').strip().upper() != 'RESTAURER':
            return
        ok, msg = self.db.restaurer(chemin)
        (messagebox.showinfo if ok else messagebox.showerror)("Restauration", msg, parent=self.outer)
        if ok:
            self.app.refresh_all()


# ============================================================ APPLICATION
class ErrorTab:
    """Onglet de remplacement quand un écran n'a pas pu être construit : le reste du logiciel reste utilisable."""
    def __init__(self, nb, titre, exc):
        self.outer = ttk.Frame(nb, padding=30)
        ttk.Label(self.outer, text=f"L'écran « {titre} » n'a pas pu s'ouvrir.", style='Title.TLabel').pack(anchor='w')
        ttk.Label(self.outer, text=f"{type(exc).__name__} : {exc}\n\nLe détail est enregistré dans le fichier erreurs.log.\n"
                                   "Les autres onglets fonctionnent normalement.", wraplength=700, justify='left').pack(anchor='w', pady=12)


class App:
    def __init__(self):
        self.root = tk.Tk(); self.root.title(D.get_config()['boutique'] + " - Gestion"); self.root.geometry("1280x780")
        apply_theme(self.root)
        self.root.minsize(980, 600)
        try:
            self.root.state('zoomed')                     # plein écran sous Windows
        except tk.TclError:
            try:
                self.root.attributes('-zoomed', True)
            except tk.TclError:
                pass
        self.root.report_callback_exception = self._on_error
        self.user, self.server, self.net = None, None, NET.load_net()
        self.db = self.open_database()
        if self.db is None or not self.login():
            self.stop_server(); self.root.destroy(); return
        self.build()
        self.root.protocol("WM_DELETE_WINDOW", self.quit)
        self.root.mainloop()
        if getattr(self, '_restart', False):                       # redémarrage demandé (ex. activation du mode téléphone)
            try:
                cmd = [sys.executable] if getattr(sys, 'frozen', False) else [sys.executable, os.path.abspath(sys.argv[0])]
                subprocess.Popen(cmd, cwd=os.path.dirname(os.path.abspath(sys.argv[0])) or None)
            except OSError:
                logging.error("Redémarrage impossible", exc_info=True)

    def _on_error(self, exc_type, exc, tb):
        """Toute erreur d'un bouton est affichée clairement au lieu de passer inaperçue."""
        if exc_type is KeyboardInterrupt:
            return
        logging.error(''.join(traceback.format_exception(exc_type, exc, tb)))
        friendly_error(self.root, exc)

    def open_database(self):
        mode = self.net['mode']
        if mode == 'client':
            rdb = NET.RemoteDatabase(self.net['host'], self.net['port'], self.net['code'])
            try:
                info = rdb.hello(); rdb.sync_config()
                return rdb
            except (NET.NetworkError, NET.AuthError) as e:
                if not messagebox.askyesno("Réseau", f"{e}\n\nTravailler seul sur cet ordinateur (poste unique) pour cette fois ?"):
                    return None
                self.net = dict(self.net, mode='autonome')
        real = Database()
        if mode == 'serveur':
            code = self.net['code'] or NET.new_code()
            self.server = NET.HostServer(lambda: real, code, self.net['port'])
            try:
                self.server.start()
            except OSError as e:
                messagebox.showwarning("Réseau", f"Le serveur n'a pas pu démarrer (port {self.net['port']} déjà utilisé ?)\n{e}\n\nL'application fonctionne en poste unique.")
                self.server = None
                return real
            return NET.LockedDB(real)
        return real

    def stop_tunnel(self):
        t = getattr(self, 'tunnel', None)
        if t:
            try:
                t.arreter()
            except Exception:
                pass
            self.tunnel = None

    def restart(self):
        self._restart = True
        self.quit()

    def phone_help(self):
        if MB is None:
            messagebox.showerror("Téléphone", "Le fichier mobile_boutique.py est absent du dossier de l'application.", parent=self.root); return
        if self.net['mode'] == 'client':
            messagebox.showinfo("Téléphone", "Cette fonction se règle sur l'ordinateur serveur de la boutique.", parent=self.root); return
        TelephoneDialog(self.root, self)

    def stop_server(self):
        if self.server:
            self.server.stop(); self.server = None

    def login(self):
        dlg = tk.Toplevel(self.root); dlg.title("Connexion"); dlg.configure(bg=THEME['bg']); dlg.resizable(False, False)
        dlg.geometry("+%d+%d" % (self.root.winfo_screenwidth() // 2 - 190, self.root.winfo_screenheight() // 3 - 120))
        dlg.update_idletasks()
        try:
            dlg.wait_visibility(); dlg.grab_set()
        except tk.TclError:
            pass
        f = ttk.Frame(dlg, padding=(34, 24)); f.pack()
        ttk.Label(f, text=D.get_config()['boutique'], style='Title.TLabel', wraplength=300, justify='center').pack(pady=(0, 2))
        ttk.Label(f, text="Connectez-vous pour continuer", style='Muted.TLabel').pack(pady=(0, 16))
        u, p = tk.StringVar(), tk.StringVar()
        ttk.Label(f, text="Identifiant").pack(anchor='w'); eu = ttk.Entry(f, textvariable=u, width=30, font=(FONT, 11)); eu.pack(fill='x', pady=(2, 10)); eu.focus_set()
        ttk.Label(f, text="Mot de passe").pack(anchor='w')
        row = ttk.Frame(f); row.pack(fill='x', pady=(2, 0))
        ep = ttk.Entry(row, textvariable=p, show='\u2022', width=26, font=(FONT, 11)); ep.pack(side='left', fill='x', expand=True)
        def toggle():
            ep.config(show='' if ep.cget('show') else '\u2022')
        Button(row, text="Voir", style='Soft.TButton', command=toggle).pack(side='left', padx=(6, 0))
        msg = ttk.Label(f, foreground=THEME['bad'], wraplength=300, justify='left'); msg.pack(anchor='w', pady=6)
        done = {'ok': False, 'busy': False}

        def go(_=None):
            if done['busy']:
                return
            done['busy'] = True
            try:
                if not u.get().strip() or not p.get():
                    msg.config(text="Saisissez votre identifiant et votre mot de passe."); return
                try:
                    r = self.db.authenticate(u.get().strip(), p.get())
                except (NET.NetworkError, NET.AuthError, NET.SessionError, sqlite3.Error) as e:
                    msg.config(text=f"Connexion impossible : {e}"); return
                if not r:
                    msg.config(text=self.db.auth_msg or "Identifiant ou mot de passe incorrect."); p.set(''); ep.focus_set(); return
                if r['must_change'] and not self.force_change(dlg, r):
                    return
                self.user = r; self.db.actor, self.db.actor_role = r['username'], r['role']
                done['ok'] = True; dlg.destroy()
            finally:
                done['busy'] = False
        for w in (eu, ep):
            w.bind('<Return>', go)
        Button(f, text="Se connecter", style='Accent.TButton', command=go).pack(fill='x', pady=(4, 0))
        dlg.bind('<Escape>', lambda e: dlg.destroy())
        dlg.protocol("WM_DELETE_WINDOW", dlg.destroy)
        self.root.wait_window(dlg)
        return done['ok']

    def force_change(self, parent, r):
        messagebox.showinfo("Sécurité", "Vous utilisez un mot de passe par défaut : veuillez en choisir un nouveau.", parent=parent)
        old = simpledialog.askstring("Mot de passe", "Mot de passe actuel :", show='*', parent=parent)
        new = old and simpledialog.askstring("Mot de passe", "Nouveau mot de passe (8 caractères min., lettres + chiffres) :", show='*', parent=parent)
        if not new:
            return False
        ok, m = self.db.change_password(r['username'], old, new)
        if not ok:
            messagebox.showerror("Mot de passe", m, parent=parent)
        return ok

    def build(self):
        head = tk.Frame(self.root, bg=THEME['head']); head.pack(fill='x')
        top = tk.Frame(head, bg=THEME['head']); top.pack(fill='x', padx=14, pady=9)
        try:                                                       # logo de l'entreprise (décoration : ne doit jamais empêcher le démarrage)
            lg = (D.get_config().get('logo') or '').strip()
            if lg and os.path.isfile(lg):
                img = tk.PhotoImage(file=lg)
                f_ = max(1, -(-img.height() // 38))
                self._logo_img = img.subsample(f_, f_)
                tk.Label(top, image=self._logo_img, bg=THEME['head']).pack(side='left', padx=(0, 10))
        except Exception:
            pass
        tk.Label(top, text=D.get_config()['boutique'], bg=THEME['head'], fg='white', font=(FONT, 15, 'bold')).pack(side='left')
        mode = {'serveur': f"SERVEUR  port {self.net['port']}", 'client': f"CLIENT  {self.net['host']}"}.get(self.net['mode'])
        if mode:
            tk.Label(top, text="  " + mode + "  ", bg=THEME['ok'], fg='white', font=(FONT, 8, 'bold')).pack(side='left', padx=14)
        Button(top, text="Quitter", style='Ghost.TButton', command=self.quit).pack(side='right')
        if self.user['role'] in ('admin', 'gerant'):
            Button(top, text="Téléphone", style='Ghost.TButton', command=self.phone_help).pack(side='right', padx=(0, 6))
        Button(top, text="Déconnexion", style='Ghost.TButton', command=self.logout).pack(side='right', padx=6)
        Button(top, text="Mon mot de passe", style='Ghost.TButton', command=self.change_my_password).pack(side='right')
        Button(top, text="Actualiser  (F5)", style='Ghost.TButton', command=self.refresh_all).pack(side='right', padx=6)
        tk.Label(top, text=f"{self.user['full_name']}   \u00b7   {ROLES[self.user['role']]}", bg=THEME['head'], fg='#cbd5e1', font=(FONT, 10)).pack(side='right', padx=14)
        # la barre d'état est placée AVANT le contenu : elle garde sa place même si l'écran est petit
        self.status = ttk.Label(self.root, style='Status.TLabel', anchor='w'); self.status.pack(fill='x', side='bottom')
        self.nb = ttk.Notebook(self.root); self.nb.pack(fill='both', expand=True, padx=10, pady=(8, 4))
        r = self.user['role']
        everyone = ('admin', 'gerant', 'magasinier', 'vendeur')
        spec = [("Accueil", AccueilTab, everyone),
                ("Vente", VenteTab, ('admin', 'gerant', 'vendeur')), ("Factures", HistoriqueTab, ('admin', 'gerant', 'vendeur')),
                ("Catalogue", ArticlesTab, everyone), ("Stock", StockTab, ('admin', 'gerant', 'magasinier')),
                ("Achats", AchatsTab, ('admin', 'gerant', 'magasinier')), ("Clients", ClientsTab, ('admin', 'gerant', 'vendeur')),
                ("Caisse", CaisseTab, ('admin', 'gerant', 'vendeur')), ("Rapports", RapportsTab, ('admin', 'gerant')),
                ("Administration", AdminTab, ('admin', 'gerant'))]
        self.tabs, self.tab_titles = [], []
        self.tab_by_title = {}
        for title, cls, roles in spec:
            if r in roles:
                try:
                    t = cls(self.nb, self)
                except Exception as e:
                    logging.error(f"Onglet {title} : construction impossible", exc_info=True)
                    t = ErrorTab(self.nb, title, e)
                self.nb.add(t.outer, text="  " + title + "  ")
                self.tabs.append(t); self.tab_titles.append(title); self.tab_by_title[title] = t
        self.nb.bind('<<NotebookTabChanged>>', self.on_tab)
        if 'Accueil' in self.tab_by_title and hasattr(self.tab_by_title['Accueil'], 'load'):
            self.tab_by_title['Accueil'].load()
        self.root.bind('<F5>', lambda e: self.refresh_all())
        for i in range(min(9, len(self.tabs))):
            self.root.bind(f'<Control-Key-{i + 1}>', lambda e, i=i: self.nb.select(i))
        self.tick()
        self.schedule_backup()

    def change_my_password(self):
        old = simpledialog.askstring("Mot de passe", "Mot de passe actuel :", show='*', parent=self.root)
        new = old and simpledialog.askstring("Mot de passe", "Nouveau mot de passe (8 caractères min., lettres + chiffres) :", show='*', parent=self.root)
        if new:
            ok, msg = self.db.change_password(self.user['username'], old, new)
            (messagebox.showinfo if ok else messagebox.showerror)("Mot de passe", msg, parent=self.root)

    def logout(self):
        """Change d'utilisateur sans fermer le logiciel (utile quand plusieurs personnes se relaient à la caisse)."""
        if not messagebox.askyesno("Déconnexion", f"Fermer la session de {self.user['full_name']} ?", parent=self.root):
            return
        for job in (getattr(self, '_bk_job', None), getattr(self, '_tick_job', None)):
            if job:
                try:
                    self.root.after_cancel(job)
                except tk.TclError:
                    pass
        self._bk_job = self._tick_job = None
        for w in self.root.winfo_children():
            w.destroy()
        self.db.actor, self.db.actor_role = 'système', None
        self.user = None
        if self.login():
            self.build()
        else:
            self.quit()

    def goto(self, title, focus=False):
        """Ouvre un onglet par son nom (utilisé par les raccourcis de l'accueil)."""
        if title in self.tab_titles:
            self.nb.select(self.tab_titles.index(title))
            t = self.tab_by_title[title]
            if focus and hasattr(t, 'entry'):
                t.entry.focus_set()

    def call_tab(self, title, method):
        self.goto(title)
        fn = getattr(self.tab_by_title.get(title), method, None)
        if fn:
            fn()

    def schedule_backup(self):
        """Sauvegarde automatique (poste serveur ou autonome) : seulement si des données ont changé depuis la dernière."""
        if self.net['mode'] == 'client':
            return
        if getattr(self, '_bk_job', None):
            try:
                self.root.after_cancel(self._bk_job)
            except tk.TclError:
                pass
        minutes = max(5.0, float(D.get_config().get('sauvegarde_auto_min') or 20))

        def run():
            try:
                changes = self.db.conn.total_changes
                if changes != getattr(self, '_bk_changes', None):
                    self.db.backup(keep=40)
                    self._bk_changes = changes
                    ok, msg = getattr(self.db, 'ext_etat', (None, ''))
                    self.status.config(text=f"  Sauvegarde automatique {datetime.now():%H:%M}" + ("" if ok is not False else f"  \u2014  ATTENTION : {msg}"))
            except Exception:
                logging.error("Sauvegarde automatique impossible", exc_info=True)
            self._bk_job = self.root.after(int(minutes * 60000), run)
        self._bk_job = self.root.after(15000, run)

    def tick(self):
        """Barre d'état : date/heure, poste, rappel des raccourcis."""
        try:
            self.status.config(text=f"  {datetime.now():%d/%m/%Y  %H:%M}   |   {self.user['full_name']}   |   "
                                    f"F2 recherche   F5 actualiser   F9 encaisser   Ctrl+1..9 onglets   Échap fermer")
            self._tick_job = self.root.after(30000, self.tick)
        except tk.TclError:
            pass

    def on_tab(self, _=None):
        """À chaque changement d'onglet on relit les données (utile quand d'autres postes travaillent en même temps)."""
        cur = self.nb.nametowidget(self.nb.select())
        for t in self.tabs:
            if t.outer is cur:
                for fn in ('load', 'refresh_results'):
                    if hasattr(t, fn):
                        try:
                            getattr(t, fn)()
                        except (tk.TclError, NET.NetworkError, NET.SessionError) as e:
                            if not isinstance(e, tk.TclError):
                                self.status.config(text=f"  Réseau indisponible : {e}")
                        break

    def refresh_all(self, except_=None):
        for t in self.tabs:
            if t is except_:
                continue
            for fn in ('load', 'refresh_results'):
                if hasattr(t, fn):
                    try:
                        getattr(t, fn)()
                    except (tk.TclError, NET.NetworkError, NET.SessionError):
                        pass
                    break

    def quit(self):
        try:
            if self.net['mode'] != 'client':       # la sauvegarde est réservée au poste qui détient la base
                self.db.backup(keep=30)
        except Exception:
            logging.error("Sauvegarde à la fermeture impossible", exc_info=True)
        self.stop_tunnel()
        self.stop_server()
        try:
            self.db.close()
        except Exception:
            pass
        self.root.destroy()


def verifier_installation():
    """Retourne la liste des fichiers manquants ou trop anciens (copie incomplète lors d'une mise à jour)."""
    pb = list(_MANQUANTS)
    if not hasattr(D, 'plan_payments') or not hasattr(D.Database, 'tableau_de_bord'):
        pb.append('database_boutique.py (ancienne version)')
    if not hasattr(NET, 'BLOCKED') or 'restaurer' not in NET.BLOCKED or not hasattr(NET.HostServer, 'invoke') or not hasattr(NET._Handler, '_origine'):
        pb.append('reseau_boutique.py (ancienne version)')
    return pb


def main():
    try:
        pb = verifier_installation()
        if pb:
            raise RuntimeError("Fichiers à copier ou à remplacer dans le dossier de l'application :\n  - " + "\n  - ".join(pb) +
                               "\n\nCopiez ensemble TOUS les fichiers fournis (main_boutique.py, database_boutique.py, reseau_boutique.py, facture_boutique.py, mobile_boutique.py, tunnel_boutique.py).")
        App()
    except Exception as e:
        logging.error("Échec au démarrage", exc_info=True)
        detail = traceback.format_exc()
        try:
            racine = getattr(tk, '_default_root', None) or tk.Tk()
            for w in racine.winfo_children():
                w.destroy()
            racine.withdraw()
            journal = os.path.join(D.BASE_DIR, 'erreurs.log')
            messagebox.showerror("Démarrage impossible", f"L'application n'a pas pu démarrer.\n\n{type(e).__name__} : {e}\n\n"
                                 f"Le détail est enregistré dans :\n{journal}\nTransmettez ce message au support.", parent=racine)
            racine.destroy()
        except Exception:
            print(detail)


if __name__ == '__main__':
    main()
