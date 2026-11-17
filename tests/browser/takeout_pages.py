from __future__ import annotations

NNBSP = " "
COMPLETED_ID = "0a1b2c3d-1111-4aaa-8bbb-000000000001"
EXPIRED_ID = "0a1b2c3d-2222-4aaa-8bbb-000000000002"
IN_PROGRESS_ID = "0a1b2c3d-3333-4aaa-8bbb-000000000003"
MADE_UP_USER = "100000000000000000001"
ROW_LABEL = "See the details and actions available for this archive, including downloads"
GB = 1024**3
LIVE_BASE = "https://takeout.google.com/"
MANAGE_PAGE = "https://takeout.google.com/manage"


def base_tag(base: str | None) -> str:
    return "" if base is None else f'<base href="{base}">'


def archive_url(google_id: str) -> str:
    return f"https://takeout.google.com/manage/archive/{google_id}"


def row(google_id: str = COMPLETED_ID, state: str = "Completed", created: str = "Created 1 hour ago",
        available: str | None = f"Available until November 24, 2026 at 1:55{NNBSP}PM", href: str | None = None) -> str:
    until = "" if available is None else f'<div class="Xq9Lb">{available}</div>'
    return (f'<li class="K6ZZTd"><div jscontroller="FS1FEb"><a class="Vw3Rf" href="{href or archive_url(google_id)}" aria-label="{ROW_LABEL}">'
            f'<div class="p1"><div class="p2"><div class="pn">Google Photos</div><div><div><div><svg viewBox="0 0 24 24"><path d="M0 0"></path></svg></div>'
            f'<p class="st">{state}</p></div></div><div class="ds">Data backup to a download link<div class="cr">{created}</div>{until}</div></div>'
            f'<div><div><span aria-hidden="true"><svg viewBox="0 0 24 24"></svg></span></div></div></div></a></div><hr></li>')


def completed_row(**changes: str) -> str:
    return row(**changes)


def expired_row(google_id: str = EXPIRED_ID, created: str = f"Created November 9, 2:45{NNBSP}PM") -> str:
    return row(google_id, "Expired", created, None)


def in_progress_row(google_id: str = IN_PROGRESS_ID, created: str = "Created 5 minutes ago") -> str:
    return row(google_id, "In progress", created, None)


def summary(*rows: str, scheduled: bool = True, body: str | None = None, base: str | None = None) -> str:
    banner = ('<div class="PEJjGd">You have 11 more scheduled exports</div><div scheduled-job-id="0a1b2c3d-9999-4aaa-8bbb-000000000009">'
              '<p>Export 2 of 12 will start on December 17, 2026</p></div><button aria-label="Cancel scheduled exports" data-job-id="0a1b2c3d-9999-4aaa-8bbb-000000000009">'
              '<i aria-hidden="true">cancel</i><span>Cancel scheduled exports</span></button>') if scheduled else ""
    listed = f'<h2 class="CrwUZc">Status</h2><ul class="OZ7tnf" jsname="archivelist">{"".join(rows)}</ul><hr>' if body is None else body
    return f"""<!doctype html><html lang="en"><head>{base_tag(base)}<title>Google Takeout</title><script nonce="">var trap = "<a href='https://takeout.google.com/manage/archive/trap-id'>Completed</a>";</script></head>
<body><a href="#main">Skip to main content</a><header><a aria-label="Google Account: Jo Example (jo@example.test)" href="https://accounts.google.com/SignOutOptions">Account</a></header>
<div role="navigation"><button aria-label="Back to previous page"></button><h1 class="cdaFgf" jsname="r4nke" tabindex="-1">Summary</h1></div>
<div role="main"><c-wiz><div><div>The status of the requests you’ve made within the last 30 days</div><div>{banner}</div><div><div><div>{listed}</div></div></div>
<div><span aria-hidden="true">Create new request</span><a href="https://takeout.google.com/" aria-label="Create new request"></a></div></div></c-wiz></div>
<footer role="contentinfo"><a href="https://policies.google.com/privacy">Privacy</a><a href="https://policies.google.com/terms">Terms</a></footer></body></html>"""


def part_names(count: int) -> list[str]:
    groups = (4, 2, 3, 1, 5)
    names = [f"takeout-20261117T125500Z-{groups[index // 5 % len(groups)]}-{index % 5 + 1:03d}.zip" for index in range(count)]
    return names


def download_href(google_id: str, index: int, user: str = MADE_UP_USER, form: str = "https://takeout.google.com/takeout/download?{query}") -> str:
    return form.format(query=f"j={google_id}&amp;i={index}&amp;user={user}")


def items_linked(form: str, google_id: str = COMPLETED_ID, sizes: tuple[int, ...] | None = None) -> list[str]:
    sizes = sizes or SIZES
    names = part_names(len(sizes))
    return [part_item(google_id, index, len(sizes), size, names[index], href=download_href(google_id, index, form=form)) for index, size in enumerate(sizes)]


def part_item(google_id: str, index: int, count: int, size: int, filename: str, href: str | None = None, size_attribute: bool = True,
              label: str | None = None) -> str:
    shown = f"{size / GB:.2f} GB"
    attribute = f' data-size="{size}"' if size_attribute else ""
    aria = label if label is not None else f"Download part {index + 1} of {count}"
    return (f'<li class="pr"><div><div><div class="pt">Part {index + 1}</div><div><div class="ps">{shown}</div></div></div><div><div>'
            f'<div data-is-webview="false" data-download-uri="takeout/download?j={google_id}&amp;i={index}&amp;user={MADE_UP_USER}"{attribute} data-job-id="{google_id}" '
            f'data-index="{index}" data-filename="{filename}" data-is-downloaded="false"><div jsname="Rfwdsf"><i aria-hidden="true">file_download</i>'
            f'<span aria-hidden="true">Download</span><a jsname="hSRGPd" href="{href or download_href(google_id, index)}" aria-label="{aria}"></a></div></div></div></div></div><hr></li>')


SIZES = (39_209_062_486, 52_525_636_975, 9_233_726_362)


def latest_export_card(google_id: str) -> str:
    return (f'<h2>Your exports</h2><div>Your latest export</div><div>Google Photos on November 17, 2026</div><div data-download-uri="takeout/download?j={google_id}&amp;i=0&amp;user={MADE_UP_USER}" '
            f'data-size="1" data-index="0"><a href="{download_href(google_id, 7)}" aria-label="Download part 8 of 9"></a></div>')


def archive(google_id: str = COMPLETED_ID, status: str = "Completed", created: str = "November 17, 2026", expires: str | None = "November 24, 2026",
            sizes: tuple[int, ...] = SIZES, items: list[str] | None = None, stacked: bool = False, loading: bool = False, base: str | None = None) -> str:
    names = part_names(len(sizes))
    parts = items if items is not None else [part_item(google_id, index, len(sizes), size, names[index]) for index, size in enumerate(sizes)]
    zips = ", ".join(f"{name} (Number of times already downloaded: 0)" for name in names)
    expiry = "" if expires is None else f"<li>Expiration date: {expires}</li>"
    summary_items = "" if loading else f"""<ul>
<li>Overall status: {status}</li>
<li>Creation date: {created}</li>
{expiry}
<li>Downloadable Zips: {zips}</li>
<li>Total size of your export: {sum(sizes) / 1e9:.2f} GB</li>
<li>Services included in your export: Google Photos ({sum(sizes) / 1e9:.2f} GB)</li>
</ul>"""
    report = (f'<li><div><div><div>{status}</div><div><section><span>Google Photos</span> · Created 1 hour ago · Available until November 24, 2026 at 1:55{NNBSP}PM</section>'
              f'<section><span aria-hidden="true">See report</span><a href="{download_href(google_id, len(sizes))}" aria-label="See report"></a></section></div></div></div><hr></li>')
    section = "" if loading else (f'<div><h2><div>Data backup to a download link</div><p><div>Your total request size is 1,101.78 GB. This request has been divided into {len(sizes)} files.</div>'
                                  f'<div>Avoid downloading your data backup onto public computers or saving them where others can see them.</div></p></h2><ul>{"".join(parts)}</ul><hr></div>')
    before = latest_export_card(google_id) if stacked else ""
    return f"""<!doctype html><html lang="en"><head>{base_tag(base)}<title>Google Takeout</title><script>var x = 'Overall status: Expired';</script></head>
<body><header><a aria-label="Google Account: Jo Example (jo@example.test)" href="https://accounts.google.com/SignOutOptions">Account</a></header>{before}
<div role="navigation" data-back-url="manage"><button aria-label="Back to previous page"></button><h1 jsname="r4nke" tabindex="-1"><c-wiz>Google Photos</c-wiz></h1></div>
<div role="main"><c-wiz><div><div data-export-type="1" data-download-quota-exceeded="false" data-job-id="{google_id}" data-retry-archive-count="0"><div><div><div>
<h2><div>SUMMARY</div></h2><ul><div><div><div style="display: none;"><div>Loading export summary...</div></div><div><span>Export summary</span></div><div>{summary_items}</div></div></div>{report}</ul><hr></div>
{section}</div></div></div></c-wiz></div></body></html>"""
