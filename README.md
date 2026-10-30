# books lib — A Simple Library for Developers

Live site: **https://books.chizumizu.space/**

Just a clean, no-nonsense place to find and download the books and papers every developer should read at some point. No login, no popup, no tracking — just search, click, and download the PDF.

Think of it like a small personal library that got put on the internet so everyone can use it.

---

### What does it do?

Pretty simple:

1.  You open **[books.chizumizu.space](https://books.chizumizu.space/)**
2.  You see a list of books — each row has a name and a tag like `python`, `js`, `git`, `os` etc.
3.  You can filter by category using the little tab bar on top, or just type in the search box — it does fuzzy search, so even if you type `pytn` it’ll still find `python`.
4.  Click on any book name and the PDF downloads straight away.

That’s it. That’s the whole idea.

---

### How it actually works

Not much magic behind the scenes:

*   This is a super lightweight **Node.js + Express + EJS** app. No database.
*   All the book info lives in one file — `meta_dt.json`. Each entry is just a slug, title, and a tag.
*   The actual PDFs are sitting in the `uploads/` folder.
*   The homepage (`/`) reads `meta_dt.json`, collects all the tags, and renders the table from `views/index.ejs`.
*   When you click download, it hits `/download/:slug` — the server checks the slug safely (blocks `..` / `/` tricks), finds the matching PDF in `uploads/`, and sends it to you. Nothing fancy, just secure and simple.
*   All the filtering and fuzzy search happens right in the browser with vanilla JS — no extra libraries.

There’s also a static build for hosting. Running `pnpm run build` renders the EJS to plain HTML and copies everything to `dist/` (with `uploads` inside). That `dist` folder is what gets deployed to GitHub Pages / the custom domain. It also creates a `.nojekyll` and a `404.html` so the custom domain `books.chizumizu.space` works fine.

---

### Features

*   Minimal, fast, black-and-white UI — easy to read
*   Category tabs (python, js, git, os, c, c++, go, cn, etc.) + auto-generated tags from books
*   Fuzzy search by book name / tag / serial number
*   Direct, secure PDF download by slug
*   Static-site ready for GitHub Pages + Custom Domain

---

### Tech Stack

*   **Backend:** Express 5
*   **Templating:** EJS
*   **Frontend:** Plain HTML/CSS/JS (no framework)
*   **Deployment:** GitHub Pages with Custom Domain (`CNAME` -> `books.chizumizu.space`)

---

### Project Structure

```
library/
├── index.js                # Express server — routes for / and /download/:slug
├── meta_dt.json            # The whole "database" — list of books
├── uploads/                # All the PDF files
├── views/index.ejs         # The single page UI
├── public/                 # Static assets
├── scripts/build-static.mjs# Builds static site to dist/ for Pages
├── dist/                   # Generated static site (after build)
└── CNAME                   # books.chizumizu.space
```

---

### Run it locally

```bash
# install deps
pnpm install

# dev mode (auto-reload)
pnpm run dev

# or normal start
pnpm start
```
Then open `http://localhost:8080`

To build the static version for deployment:
```bash
pnpm run build
pnpm run preview
```

---

### How to add a new book

1. Drop the PDF into `uploads/` — e.g. `my-new-book.pdf`
2. Add an entry in `meta_dt.json`:
```json
"b10": {
  "slug": "my-new-book",
  "title": "My New Book",
  "description": "this is development",
  "tag": "python"
}
```
Make sure the `slug` matches the PDF filename without `.pdf`. That’s all — it’ll show up on next reload/build.

---

### Deploy the uploader

The uploader is a separate FastAPI service that accepts PDFs and commits them to this repo. Clone this repo into your home directory, then copy the uploader out of the repo and deploy it at `~/uploader` (the library stays at `~/library`). See [uploader/README.md](uploader/README.md) for the exact steps.

---

### Visit

**https://books.chizumizu.space/** — open it, try the search, grab what you need.

If you have a book suggestion that should be in here, feel free to open an issue or PR.

*Just books. No clutter.*
