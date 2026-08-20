# MoonX DEMO — Routine de trading 24/7 gratuite (GitHub Actions)

Ce package fait tourner ta routine de trading SMC **dans le cloud, gratuitement**, même PC éteint.

## 📦 Contenu

| Fichier | Rôle |
|---|---|
| `trading_routine.py` | La routine complète (celle qui tourne déjà chez toi, testée et fonctionnelle) |
| `.github/workflows/trading.yml` | Le déclencheur horaire GitHub Actions |
| `state/` | Créé automatiquement : mémoire de la routine entre les passages |

## 🚀 Installation (10 minutes)

### 1. Créer le dépôt GitHub
1. Va sur https://github.com/new
2. Nom : `moonx-trading` (ou autre)
3. **Important : coche « Private »** 🔒
4. Clique « Create repository »

### 2. Envoyer les fichiers
Sur la page du dépôt vide, clique « **uploading an existing file** » :
- Glisse-dépose `trading_routine.py`
- Puis crée le workflow : « Add file » → « Create new file » → nom : `.github/workflows/trading.yml` → colle le contenu du fichier `trading.yml` fourni → « Commit changes »

(Alternative si tu sais utiliser git : `git init && git add -A && git commit -m "init" && git remote add origin <url> && git push -u origin main`)

### 3. Ajouter le secret (ta clé MoonX)
1. Dans le dépôt : **Settings** → **Secrets and variables** → **Actions**
2. « New repository secret »
3. Name : `MOONX_TOKEN`
4. Secret : colle ta clé (la partie après `token=` dans ton URL : `mcp_1011eaa...`)
5. « Add secret »

### 4. Activer et tester
1. Onglet **Actions** → clique « I understand my workflows, go ahead and enable them » si demandé
2. Clique sur le workflow « MoonX DEMO — Routine trading SMC » → **Run workflow** (test immédiat)
3. Clique sur le run pour voir le log : tu dois voir le résumé du passage (soldes, biais, ordres)

## ✅ C'est tout !
La routine tournera **toutes les heures à :43 UTC**, PC éteint ou non. L'onglet Actions garde l'historique de chaque passage (logs complets).

## ⚠️ À savoir
- GitHub peut retarder les crons de **quelques minutes** en période de charge — sans impact pour une stratégie horaire
- Gratuit : 2000 minutes/mois incluses ; ce workflow utilise ~2 min/passage × 24 × 30 ≈ **1440 min/mois** — ça passe
- Le dossier `state/` est re-commité automatiquement après chaque passage (mémoire break-even/partiels)
- Compte **DEMO uniquement**. La clé reste dans les Secrets GitHub (chiffrée, jamais affichée dans les logs)

## 🔄 Pour arrêter
Onglet Actions → workflow → « ... » → **Disable workflow**.
