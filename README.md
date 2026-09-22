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
| Spielautomat | 3 Walzen, 5 Gewinnlinien, dazu ein wachsender Jackpot (2 % jedes Einsatzes fließt hinein) |
| Roulette | Europäisch (eine Null), Zahlen, Rot/Schwarz, Gerade/Ungerade, Hälften, Dutzende |
| Blackjack | Gegen den Dealer, dazu die KI-Spieler Max und Luna. Blackjack zahlt 3:2, Verdoppeln und All-In möglich |
| Poker | Texas Hold'em (Fixed Limit) gegen Max (offensiv) und Luna (vorsichtig), 3 Blind-Stufen |
| Würfeln | 3 Würfel, Groß/Klein, einzelne Zahlen, Tripel bis 150:1 |

Dazu: Registrierung/Login (mit optionalem Einladungscode), Startguthaben 1.000,
Tagesbonus mit **Tagesserie** (steigt jeden Tag in Folge), **Glücksrad** (einmal täglich, unabhängig
vom Tagesbonus), **Siegesserie** mit Bonus-Auszahlung, Notgroschen bei Pleite, mehrere Bestenlisten
(Guthaben, Siegesserie, Tagesserie, größter Einzelgewinn – auch ohne Login einsehbar) und Verlauf.

## Einladungscode (optional)

Standardmäßig kann sich jeder mit der URL registrieren. Willst du das einschränken, setze beim
Start die Umgebungsvariable `INVITE_CODE`, zum Beispiel:

    INVITE_CODE=freunde123 python3 server.py

Bei Render trägst du das unter **Environment** als Variable `INVITE_CODE` ein. Ohne Code kann sich
niemand mehr registrieren, bestehende Konten können sich aber weiter normal einloggen. Du kannst den
Wert dort jederzeit ändern.

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
