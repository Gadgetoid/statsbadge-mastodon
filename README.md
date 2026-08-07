# statsbadge-mastodon

Your Mastodon timeline and account, for [statsbadge](https://github.com/pimoroni/statsbadge).

The newest post in your feed, the newest mention, the newest notification of any kind and your last post, alongside your followers, following, posts and unread count. Add a **Notifications** page and put any mixture of them on it: the messages stack down the page and the counters go in a strip along the bottom.

## Install

```bash
statsbadge ext add mastodon
```

Then, in the config UI under **Extensions**, set your instance and paste your access token.

## The token

In Mastodon: **Preferences → Development → New application**. Give it a name, leave the scopes at `read` (that is all this asks for), and save. The application's page then shows **Your access token**.

Paste that and your instance's host - `fosstodon.org`, no `https://` - into the settings. The token is stored in the host's config file in plain text. The config page keeps it masked behind **Edit secrets**.

## Settings

| Setting | What it does |
| ------- | ------------ |
| Instance | The host your account is on |
| Access token | The token above |
| Ask every | Seconds between refreshes. 120 by default |
| Pictures | `off`, `small` or `large` - see below |

## Pictures

One picture per post, where the post has one: the first attachment, cropped to what is actually in it and drawn in the theme's palette.

`small` is 64x48 in four shades and adds about 950 bytes to a message; `large` is 128x96 in eight and adds about 4KB. Four subsequent posts with images might cause animation hiccups so choose carefully.

## What it reports

Four of these are messages, and go on a Notifications page as a block of text with who it is from and how long ago:

| Reading | What it is |
| ------- | ---------- |
| Latest in your feed | The newest post on your home timeline |
| Latest mention | The newest post that mentioned you |
| Latest notification | The newest of any kind - a follow, a favourite, a boost |
| Your latest post | Your own newest, replies and boosts excluded |

The rest are numbers, and go anywhere a number goes:

| Reading | What it is |
| ------- | ---------- |
| Followers | |
| Following | |
| Posts | |
| Unread notifications | |
| Likes on your latest | |
| Boosts on your latest | |

Followers and following are kept **hourly**, so a graph of either shows a week rather than the last ninety seconds. Mastodon reports no history of its own, so that ring is built as the host runs: it is empty on a first launch and fills an hour at a time.

## Notes

Four requests a refresh, against a limit of 300 in five minutes, so the default of two minutes is 1/40th of the API limit.

A boost includes no content of its own - the content is on the post inside it - so the original post is drawn with "boosted by" beside it.

Post bodies arrive as HTML. Tags go, entities are decoded, and paragraph and line breaks become spaces rather than being dropped. They are cut to 160 characters on the server, comfortably more than the two or three lines a page draws.

Mastodon "readings" change every couple of minutes and the badge polls every second, so they are declared slow: the host sends them when they change and the badge holds on to them.
