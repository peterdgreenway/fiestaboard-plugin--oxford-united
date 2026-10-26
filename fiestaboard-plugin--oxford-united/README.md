# Oxford United Plugin

FiestaBoard plugin for **Oxford United** (English League One).

Shows league table position, **last match with half-time & full-time scores**, **scorers + minute**, and next fixture.

Data source: free [TheSportsDB](https://www.thesportsdb.com/) API (same as the official Sports Scores plugin).

## Features

- Current League One position, points, W/D/L, GD, form
- Last match: full-time score, result (W/D/L)
- **Half-time score** (derived from timeline goals with minute ≤ 45)
- **Scorers with minute** (e.g. `Cheek 67'`)
- Next upcoming fixture (date + kick-off)
- Pre-formatted 22-character lines for the Vestaboard
- Optional premium TheSportsDB API key

## New in v1.1

| Variable | Description | Example |
|----------|-------------|---------|
| `ht_score` | Half-time score | `0-0` |
| `ft_score` | Full-time score | `1-0` |
| `formatted_htft` | Combined HT + FT line | `HT 0-0  FT 1-0` |
| `scorers` / `formatted_scorers` | Compact scorer list | `Cheek 67'` |
| `scorer_1` … `scorer_4` | One scorer per line | `67' Cheek` |
| `goals` (array) | Structured goals | `{{oxford_united.goals.0.formatted}}` |

## Template Variables (main)

### Table
| Variable | Example |
|----------|---------|
| `{{oxford_united.formatted_table}}` | `OXF  8th  18pts` |
| `{{oxford_united.position}}` | `8` |
| `{{oxford_united.points}}` | `18` |
| `{{oxford_united.form}}` | `WWDLW` |

### Last match scores
| Variable | Example |
|----------|---------|
| `{{oxford_united.formatted_last}}` | `OXF 1-0 BRA` |
| `{{oxford_united.ht_score}}` | `0-0` |
| `{{oxford_united.ft_score}}` | `1-0` |
| `{{oxford_united.formatted_htft}}` | `HT 0-0  FT 1-0` |
| `{{oxford_united.last_result}}` | `W` |

### Scorers
| Variable | Example |
|----------|---------|
| `{{oxford_united.formatted_scorers}}` | `Cheek 67'` |
| `{{oxford_united.scorer_1}}` | `67' Cheek` |
| `{{oxford_united.goals.0.player}}` | `Cheek` |
| `{{oxford_united.goals.0.minute}}` | `67` |
| `{{oxford_united.goals.0.formatted}}` | `67' Cheek` |

### Next fixture
| Variable | Example |
|----------|---------|
| `{{oxford_united.formatted_next}}` | `NEXT OXF vs STK` |
| `{{oxford_united.next_date}}` | `2026-10-27` |
| `{{oxford_united.next_time}}` | `19:45` |

## Example pages

**Match report style**
```
{center}OXFORD UNITED
{{oxford_united.formatted_table}}
{{oxford_united.formatted_last}}
{{oxford_united.formatted_htft}}
{{oxford_united.formatted_scorers}}
Form {{oxford_united.form}}
```

**Scorers on separate lines**
```
{center}LAST MATCH
{{oxford_united.formatted_last}}
{{oxford_united.formatted_htft}}
{{oxford_united.scorer_1}}
{{oxford_united.scorer_2}}
{{oxford_united.scorer_3}}
```

## Configuration

| Setting | Default | Description |
|---------|---------|-------------|
| `api_key` | `123` | TheSportsDB key (free works) |
| `refresh_seconds` | `300` | Poll interval (min 120) |
| `show_form` | `true` | Include form string |
| `max_scorers` | `4` | Max names in `formatted_scorers` |

## How scores are built

- **Full-time**: `intHomeScore` / `intAwayScore` from the event
- **Half-time**: counted from timeline goals where `minute ≤ 45` (own goals credit the opposing side)
- **Scorers**: timeline entries of type Goal / Penalty / Own Goal, ordered by minute

If TheSportsDB has not yet populated the timeline, HT will show `0-0` (or blank) and the scorer list will be empty while FT remains available.

## Installation

```bash
curl -X POST http://localhost:4420/api/plugins/install \
  -H "Content-Type: application/json" \
  -d '{"repository": "https://github.com/YOUR_USER/fiestaboard-plugin--oxford-united"}'
```

Then enable **Oxford United** on the Integrations page.

## Author

Community plugin for FiestaBoard.


## Force-refresh scores on the dashboard

Plugins cannot inject custom buttons into the FiestaBoard UI.

**Fastest method:** Integrations → Oxford United → **Save Changes**.  
That runs `on_config_change`, clears the cache, and the next update shows new scores.

Or via API:

```bash
curl -s http://localhost:4420/api/plugins/oxford_united/data | jq .
```
