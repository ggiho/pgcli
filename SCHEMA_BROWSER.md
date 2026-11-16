# Schema browser and commands without function keys

Run `\browse` in the interactive prompt. Type a schema/table name and use arrows
to choose it. Enter moves into the column inspector; it does not load or run SQL.

The detail pane has three views, reachable with Tab/Shift+Tab or a mouse click:

- **Columns**: aligned column names, types, primary keys and nullability. Type in
  Find column to filter by name/type. Select a row to see its full type, default
  and comment below the grid.
- **Indexes**: index names and their definitions, separate from column data.
- **SQL**: highlighted server definitions. MySQL shows CREATE SQL; PostgreSQL
  shows catalog constraints/indexes and view queries, without pretending these
  are a complete CREATE TABLE script. Ctrl+F searches this SQL view.

Filters and selection survive view switches. Escape from details returns to table
search with its caret preserved; Escape there closes. In list navigation, j/k
move, f or / focuses search, and h/l moves between the table and column lists.
Typed text is never intercepted by these navigation keys.

**SELECT draft** fills `SELECT * ... LIMIT 100;` in the query prompt. **Insert name**
fills a quoted qualified identifier. Neither action executes SQL. The terminal
background and configured SQL colors are retained; wide screens split the two
panes, while narrow screens put a compact table list above the detail area.

Metadata is loaded only on demand through separate connections with bounded
connect/read or statement timeouts. The browser never drains an existing result
or commits the main connection. Listing covers at most 2,000 accessible non-system
objects; temporary/session-only objects are excluded. Search covers that list and
the selected object's structure, not all columns in every database at once.
Rapid movement is debounced; selected structure is cached for the browser lifetime.
Errors stay visible and the browser remains closable while metadata is loading.

Function keys are no longer bound. Use these typed commands:

| Command | Action |
| --- | --- |
| `\keys` | List the available interactive commands |
| `\help` (mycli), `\?` (pgcli) | CLI help |
| `\smart` | Toggle smart completion |
| `\multiline` | Toggle multiline input |
| `\mode` | Toggle Vi/Emacs editing |
| `\explain SQL` (mycli) | Preview the supplied statement's EXPLAIN |
| `\explain` (pgcli) | Toggle the existing EXPLAIN ANALYZE mode (executes SQL) |
| `\browse` | Open the schema browser |

These UI commands are interactive-only. Existing Ctrl+T, history and text editing
keys remain available. No new terminal-global shortcut or Ctrl/Alt chord is added.
