# Salon Émeraude – Spielgeld-Casino

Ein komplettes Casino im Browser mit Konten und Datenbank.
**Es wird nur Spielgeld verwendet, es geht nie um echtes Geld.**

## Starten

Du brauchst nur Python 3.8 oder neuer. Es muss nichts installiert werden.

    python3 server.py

Dann im Browser öffnen: http://localhost:8000
(Anderer Port: `PORT=9000 python3 server.py`)

Auf Windows heißt der Befehl meist `python server.py` oder `py server.py`.

## Was drin ist

| Spiel | Details |
|---|---|
| Spielautomat | 3 Walzen, 5 Gewinnlinien, Auszahlungsquote ca. 96 % |
| Roulette | Europäisch (eine Null), Zahlen, Rot/Schwarz, Gerade/Ungerade, Hälften, Dutzende |
| Blackjack | Gegen den Dealer, dazu die KI-Spieler Max und Luna. Blackjack zahlt 3:2, Verdoppeln möglich |
| Poker | Texas Hold'em (Fixed Limit) gegen Max (offensiv) und Luna (vorsichtig), 3 Blind-Stufen |

Dazu: Registrierung/Login, Startguthaben 1.000, Tagesbonus (500 alle 24 Std.),
Notgroschen bei Pleite, Rangliste und Verlauf.

## Datenbank

Alles liegt in der SQLite-Datei `casino.db` (wird automatisch angelegt):

- `users` – Konten, Passwort-Hash (PBKDF2), Guthaben
- `sessions` – Login-Tokens
- `transactions` – jede Gewinn-/Verlustbuchung
- `active_games` – laufende Blackjack- und Poker-Runden (überstehen einen Neustart)

Alle Spiele werden auf dem Server berechnet, der Browser kann das Guthaben nicht selbst ändern.
Zum Zurücksetzen einfach `casino.db` löschen.

## Dateien

- `server.py` – Server, Datenbank, alle Spielregeln und die Poker-KI
- `index.html` – komplette Oberfläche

Hinweis: Der Server ist für den Eigengebrauch im Heimnetz gedacht, nicht für das offene Internet.
