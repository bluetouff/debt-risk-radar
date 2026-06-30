# Debt Risk Radar — deploiement Debian durci

Objectif : exposer Streamlit uniquement via Apache en HTTPS, sans jamais publier le port applicatif ni les cles API.

## 1. Utilisateur systeme

```bash
sudo adduser --system --group --home /var/lib/debt-risk-radar debt-radar
sudo install -d -o debt-radar -g debt-radar -m 700 /var/lib/debt-risk-radar
sudo install -d -o debt-radar -g debt-radar -m 755 /var/www/debt-risk-radar
sudo install -d -o debt-radar -g debt-radar -m 755 /opt/debt-risk-radar
```

## 2. Code et venv

```bash
export DEBT_RISK_RADAR_SRC="$HOME/debt-risk-radar"

sudo rsync -a --delete \
  --exclude .venv \
  --exclude __pycache__ \
  "$DEBT_RISK_RADAR_SRC"/ /opt/debt-risk-radar/

sudo chown -R root:root /opt/debt-risk-radar
sudo python3 -m venv /opt/debt-risk-radar/.venv
sudo /opt/debt-risk-radar/.venv/bin/pip install --upgrade pip
sudo /opt/debt-risk-radar/.venv/bin/pip install -r /opt/debt-risk-radar/requirements.txt
```

## 3. Secrets

```bash
sudo cp /opt/debt-risk-radar/deploy/debt-risk-radar.env.example /etc/debt-risk-radar.env
sudoedit /etc/debt-risk-radar.env
sudo chown root:debt-radar /etc/debt-risk-radar.env
sudo chmod 640 /etc/debt-risk-radar.env
```

Ne mets jamais `FRED_API_KEY` ou `MASSIVE_API_KEY` dans le code, les logs, l'historique shell ou Apache.

## 4. Service systemd

```bash
sudo cp /opt/debt-risk-radar/deploy/debt-risk-radar.service /etc/systemd/system/debt-risk-radar.service
sudo cp /opt/debt-risk-radar/deploy/debt-risk-radar-export.service /etc/systemd/system/debt-risk-radar-export.service
sudo cp /opt/debt-risk-radar/deploy/debt-risk-radar-export.timer /etc/systemd/system/debt-risk-radar-export.timer
sudo systemctl daemon-reload
sudo systemctl enable --now debt-risk-radar
sudo systemctl enable --now debt-risk-radar-export.timer
sudo systemctl start debt-risk-radar-export.service
sudo systemctl status debt-risk-radar
```

Le service ecoute uniquement sur `127.0.0.1:8502`.

Le fichier machine-readable public est ecrit dans `/var/www/debt-risk-radar/latest.json` par
`debt-risk-radar-export.service`, puis rafraichi par `debt-risk-radar-export.timer`.

## 5. Apache reverse proxy

Modules requis :

```bash
sudo a2enmod proxy proxy_http proxy_wstunnel headers ssl rewrite
sudo cp /opt/debt-risk-radar/deploy/apache-debt-risk-radar.conf /etc/apache2/sites-available/debt-risk-radar.conf
sudo a2ensite debt-risk-radar
sudo apache2ctl configtest
sudo systemctl reload apache2
```

Adapte `ServerName` et les chemins Let's Encrypt dans le fichier Apache.

Le vhost exclut `/latest.json` du reverse proxy Streamlit et le sert directement depuis
`/var/www/debt-risk-radar/latest.json`.

## 6. Pare-feu

```bash
sudo ufw default deny incoming
sudo ufw default allow outgoing
sudo ufw allow OpenSSH
sudo ufw allow "Apache Full"
sudo ufw enable
```

Le port `8502` ne doit jamais etre ouvert publiquement.

## 7. Verification

```bash
curl -I https://debt.l0g.fr/
curl -sS https://debt.l0g.fr/latest.json | python3 -m json.tool | head
curl -sS http://127.0.0.1:8502/_stcore/health
sudo journalctl -u debt-risk-radar -n 100 --no-pager
sudo journalctl -u debt-risk-radar-export -n 100 --no-pager
```

Controle attendu :

- HTTPS force.
- `X-Frame-Options: DENY`.
- `X-Content-Type-Options: nosniff`.
- `Referrer-Policy: no-referrer`.
- `/latest.json` repond en JSON valide, sans cle API.
- aucune cle API dans les logs.
- service lance sous `debt-radar`, pas root.

## 8. Mise a jour

```bash
export DEBT_RISK_RADAR_SRC="$HOME/debt-risk-radar"

sudo rsync -a --delete \
  --exclude .venv \
  --exclude __pycache__ \
  "$DEBT_RISK_RADAR_SRC"/ /opt/debt-risk-radar/
sudo chown -R root:root /opt/debt-risk-radar
sudo install -d -o debt-radar -g debt-radar -m 755 /var/www/debt-risk-radar
sudo cp /opt/debt-risk-radar/deploy/debt-risk-radar.service /etc/systemd/system/debt-risk-radar.service
sudo cp /opt/debt-risk-radar/deploy/debt-risk-radar-export.service /etc/systemd/system/debt-risk-radar-export.service
sudo cp /opt/debt-risk-radar/deploy/debt-risk-radar-export.timer /etc/systemd/system/debt-risk-radar-export.timer
sudo systemctl daemon-reload
sudo cp /opt/debt-risk-radar/deploy/apache-debt-risk-radar.conf /etc/apache2/sites-available/debt-risk-radar.conf
sudo apache2ctl configtest
sudo systemctl reload apache2
sudo systemctl restart debt-risk-radar
sudo systemctl enable --now debt-risk-radar-export.timer
sudo systemctl start debt-risk-radar-export.service
```

## Notes securite

- Streamlit reste une app serveur : garde-la derriere Apache, jamais exposee directement.
- Garde `showErrorDetails=false` en production.
- La CSP est volontairement compatible Streamlit. Tu peux la durcir apres test navigateur complet, mais ne casse pas les websockets `_stcore`.
- Le systemd fourni bloque l'ecriture partout sauf `/var/lib/debt-risk-radar` et `/var/www/debt-risk-radar`.
