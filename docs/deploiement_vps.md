# Déploiement sur un VPS

Cible : un petit VPS Linux (Debian 12 ou Ubuntu 24.04, 1 vCPU, 2 Go de RAM,
20 Go de disque suffisent largement pour l'étape 1).

## 1. Préparer le serveur

```bash
sudo apt update && sudo apt install -y python3-venv git
sudo useradd --system --create-home --home-dir /opt/comptage-train-fr comptage
sudo -u comptage git clone https://github.com/quen9669/Comptage-Train-FR.git /opt/comptage-train-fr
cd /opt/comptage-train-fr
sudo -u comptage python3 -m venv .venv
sudo -u comptage .venv/bin/pip install -e ".[dashboards]"
sudo -u comptage cp .env.example .env && sudo chmod 600 .env
```

Renseigner `PRIM_API_KEY` dans `/opt/comptage-train-fr/.env` (droits 600 : seul
l'utilisateur `comptage` peut le lire).

## 2. Vérifier les sources

```bash
sudo -u comptage .venv/bin/comptage check-sources
```

Corriger dans `config/sources.yaml` tout identifiant en `KO`, puis passer
`verified: true` sur ceux qui répondent.

## 3. Planifier la collecte

```bash
sudo cp deploy/*.service deploy/*.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now comptage-ingest-daily.timer comptage-ingest-weekly.timer
sudo systemctl start comptage-ingest@daily.service     # premier passage immédiat
sudo systemctl start comptage-ingest@weekly.service
systemctl list-timers 'comptage*'
journalctl -u 'comptage-ingest@*' -n 50
```

`Persistent=true` rattrape une collecte manquée si le serveur était éteint.

## 4. Dashboard

```bash
sudo systemctl enable --now comptage-dashboard.service
```

Le dashboard n'écoute que sur `127.0.0.1` : rien n'est exposé sur Internet.
Depuis ton ordinateur :

```bash
ssh -L 8501:127.0.0.1:8501 utilisateur@ip-du-vps
# puis ouvrir http://localhost:8501
```

## 5. Sauvegarde

Le dossier `data/` est le seul état à protéger. Une copie hebdomadaire suffit :

```bash
rsync -a utilisateur@ip-du-vps:/opt/comptage-train-fr/data/ ./sauvegarde-comptage/
```
