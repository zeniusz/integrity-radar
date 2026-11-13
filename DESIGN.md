# Integrity Radar: design plan (v2)

## Starting point
A design review of the first version found that it showed data more prominently than decisions. It needed
an investigation workflow (queue → evidence → decision), a real matchup header, a chart that marks the
suspicious window, a comparison table instead of three price cards, an always-visible decision area,
explainable scores and clear source coverage.

## Direction: "Matchday Casebook"
Two worlds meet here: the match official's language (yellow card / red card, the match sheet) and the
broadcast scorebug (condensed caps team codes, team colours). Severity uses the referee's cards:
a yellow card means "review", a red card means "escalate". That metaphor is the one bold element.
Everything else stays quiet, sharp-edged and printed-looking, like a match report.

### Why this direction
A dark background with one bright accent is the default look for an analytics dashboard, and a cream
"casebook" is a default too. So: a cool, light canvas with a navy top bar, and the referee cards supply the colour.

## Tokens
| Name | Hex | Role |
|---|---|---|
| Tunnel navy | #111B2B | Top bar, scorebug, primary text |
| Canvas | #F1F3F6 | Page background (cool, not cream) |
| Sheet | #FFFFFF | Working surfaces |
| Line | #D8DEE6 | 1px rules |
| Yellow card | #F5C400 | Review-worthy |
| Red card | #D7263D | Escalate |
| Pitch | #1E7F5C | Normal / cleared |
| Sources | Polymarket #2F5BEA, bookmaker #E07B00, Kalshi #7B4FD6 | Kept apart from severity colours |

Type: **Barlow Condensed** (600/700) for team codes, scores and probabilities, used like scoreboard
lettering. **IBM Plex Sans** (400/500/600) for everything else, with tabular numbers. No monospace, and no
all-caps labels (team codes are content, so they stay caps).

Radius: 4px for panels, 2–3px for controls. Queue cards stand upright; only the main case card keeps a 3° tilt.

## Layout (desktop ≥ 1200px)
```
┌ navy bar: wordmark | NBA NFL NHL | Live · Backtest | source coverage dots ┐
├ status strip: 13 fixtures   2 awaiting review   Polymarket 13/13  DK 11/13  Kalshi 13/13 ┤
│ queue 300px      │ evidence (flex)                         │ case 340px (sticky)   │
│ Pre-match        │ SCOREBUG  GSW ▌ @ ▐ SAS   starts in 26h │ [yellow card] 24      │
│  ▮24 GSW @ SAS   │ market table (source, prob, move, info)  │ score breakdown bars  │
│  ▮20 NYK @ IND   │ chart: shaded jump window, labelled      │ evidence / causes /   │
│ In play / final  │   reference lines, kickoff line, 24h/7d  │ checks (checkboxes)   │
│   ...            │ signals list                             │ decision + note + log │
```
On desktop the live view fits one screen: the page never scrolls, quiet fixtures fold away, and the decision buttons stay pinned at the bottom of the case panel. Below 1240px the case panel drops under the evidence; below 820px everything is one column.

## Principles
1. Decisions before data: the case panel is always visible.
2. Every number explains itself: the score breaks down into the signals behind it.
3. Sports first: team codes, team colours and kickoff countdowns before market jargon.
4. Severity is a referee's card. Nothing else uses yellow or red.
