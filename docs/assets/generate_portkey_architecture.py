#!/usr/bin/env python3
"""Generate the Portkey Hybrid reference architecture with official icons."""

from __future__ import annotations

import base64
import html
import shutil
import subprocess
from pathlib import Path

import diagrams
from diagrams.aws.compute import EKS
from diagrams.aws.general import SslPadlock
from diagrams.aws.ml import Bedrock
from diagrams.aws.network import NLB
from diagrams.aws.security import IAMRole
from diagrams.aws.storage import S3
from diagrams.k8s.compute import Deployment, Pod
from diagrams.onprem.client import Users
from diagrams.onprem.inmemory import Redis


OUTPUT_DIR = Path(__file__).resolve().parent
SVG_PATH = OUTPUT_DIR / "portkey-architecture.svg"
PNG_PATH = OUTPUT_DIR / "portkey-architecture.png"
RESOURCE_ROOT = Path(diagrams.__file__).resolve().parent.parent


def icon_data(icon_class: type) -> str:
    path = RESOURCE_ROOT / icon_class._icon_dir / icon_class._icon
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:image/png;base64,{encoded}"


def image(icon_class: type, x: int, y: int, size: int) -> str:
    data = icon_data(icon_class)
    return (
        f'<image x="{x}" y="{y}" width="{size}" height="{size}" '
        f'href="{data}" xlink:href="{data}" preserveAspectRatio="xMidYMid meet"/>'
    )


def text_lines(
    x: int,
    y: int,
    lines: list[str],
    *,
    size: int = 17,
    color: str = "#5F6B7A",
    weight: int = 400,
    anchor: str = "middle",
    line_height: int = 23,
) -> str:
    spans = []
    for index, line in enumerate(lines):
        dy = 0 if index == 0 else line_height
        spans.append(f'<tspan x="{x}" dy="{dy}">{html.escape(line)}</tspan>')
    return (
        f'<text x="{x}" y="{y}" text-anchor="{anchor}" '
        f'font-size="{size}" font-weight="{weight}" fill="{color}">'
        + "".join(spans)
        + "</text>"
    )


def card(
    x: int,
    y: int,
    width: int,
    height: int,
    icon_class: type,
    title: str | list[str],
    detail: list[str],
    *,
    border: str = "#AAB7B8",
    fill: str = "#FFFFFF",
    icon_size: int = 66,
) -> str:
    title_lines = [title] if isinstance(title, str) else title
    icon_size = min(icon_size, height - 78)
    icon_x = x + (width - icon_size) // 2
    icon_y = y + 14
    title_y = icon_y + icon_size + 24
    detail_y = title_y + 25 + (len(title_lines) - 1) * 20
    return "\n".join(
        [
            f'<rect x="{x}" y="{y}" width="{width}" height="{height}" rx="7" '
            f'fill="{fill}" stroke="{border}" stroke-width="2"/>',
            image(icon_class, icon_x, icon_y, icon_size),
            text_lines(
                x + width // 2,
                title_y,
                title_lines,
                size=17,
                color="#161E2D",
                weight=700,
                line_height=20,
            ),
            text_lines(
                x + width // 2,
                detail_y,
                detail,
                size=13,
                line_height=18,
            ),
        ]
    )


def horizontal_card(
    x: int,
    y: int,
    width: int,
    height: int,
    icon_class: type,
    title: str | list[str],
    detail: list[str],
    *,
    border: str = "#AAB7B8",
    fill: str = "#FFFFFF",
) -> str:
    title_lines = [title] if isinstance(title, str) else title
    icon_size = min(52, height - 28)
    icon_x = x + 15
    icon_y = y + (height - icon_size) // 2
    text_x = icon_x + icon_size + 15
    detail_y = y + 53 + (len(title_lines) - 1) * 17
    return "\n".join(
        [
            f'<rect x="{x}" y="{y}" width="{width}" height="{height}" rx="7" '
            f'fill="{fill}" stroke="{border}" stroke-width="2"/>',
            image(icon_class, icon_x, icon_y, icon_size),
            text_lines(
                text_x,
                y + 31,
                title_lines,
                size=15,
                color="#161E2D",
                weight=700,
                anchor="start",
                line_height=17,
            ),
            text_lines(
                text_x,
                detail_y,
                detail,
                size=12,
                anchor="start",
                line_height=16,
            ),
        ]
    )


def step_badge(x: int, y: int, number: int, *, color: str = "#146EB4") -> str:
    return (
        f'<circle cx="{x}" cy="{y}" r="17" fill="{color}"/>'
        f'<text x="{x}" y="{y + 6}" text-anchor="middle" font-size="16" '
        f'font-weight="700" fill="#FFFFFF">{number}</text>'
    )


def build_svg() -> str:
    developer = card(
        65,
        225,
        240,
        205,
        Users,
        "Developer workstation",
        ["Codex CLI", "Local files and tools"],
        border="#146EB4",
        fill="#F3FAFD",
        icon_size=72,
    )
    private_access = card(
        385,
        245,
        180,
        185,
        SslPadlock,
        ["Private DNS", "+ routing"],
        ["Corporate/VPN path", "Provided separately"],
        border="#146EB4",
        fill="#F3FAFD",
        icon_size=58,
    )
    nlb = card(
        655,
        235,
        200,
        205,
        NLB,
        "Internal IPv4 NLB",
        ["ACM TLS :443", "Prefix-list ingress only", "IP targets; no NodePort"],
        border="#8C4FFF",
        fill="#FBF8FF",
        icon_size=67,
    )
    gateway = card(
        915,
        275,
        255,
        195,
        Pod,
        ["Portkey Enterprise", "gateway"],
        ["EKS pod", "Responses API"],
        border="#ED7100",
        fill="#FFF9F3",
        icon_size=62,
    )
    bedrock = card(
        1275,
        220,
        225,
        195,
        Bedrock,
        ["Amazon Bedrock", "Mantle"],
        ["Configured project scope", "Explicit model allowlist"],
        border="#01A88D",
        fill="#F2FCFA",
        icon_size=66,
    )

    redis = horizontal_card(
        915,
        480,
        255,
        92,
        Redis,
        "Redis",
        ["ClusterIP only; no NodePort"],
        border="#C9252D",
        fill="#FFF8F8",
    )
    controller = horizontal_card(
        915,
        590,
        255,
        115,
        Deployment,
        ["AWS Load Balancer", "Controller"],
        [
            "kube-system",
            "Watches Portkey namespace",
            "Separate NLB-only IRSA",
        ],
        border="#326CE5",
        fill="#F5F8FF",
    )
    s3 = horizontal_card(
        1275,
        465,
        225,
        105,
        S3,
        "Amazon S3",
        ["Request/response logs", "Retained on stack cleanup"],
        border="#248814",
        fill="#F6FBF5",
    )
    irsa = horizontal_card(
        1275,
        600,
        225,
        100,
        IAMRole,
        "IAM / IRSA role",
        ["Model- and bucket-scoped", "No static AWS key"],
        border="#DD344C",
        fill="#FFF7F8",
    )

    return f"""<svg xmlns="http://www.w3.org/2000/svg"
  xmlns:xlink="http://www.w3.org/1999/xlink"
  width="1600" height="980" viewBox="0 0 1600 980"
  role="img" aria-labelledby="title desc">
  <title id="title">Codex through Portkey Hybrid on Amazon EKS</title>
  <desc id="desc">Codex and local tools remain on the developer workstation. The client uses customer-provided private DNS and corporate or VPN routing to reach an internal IPv4 Network Load Balancer. The load balancer accepts ACM TLS on port 443 only from approved prefix lists and forwards TCP on port 8787 to the Portkey Enterprise gateway in Amazon EKS. The Redis Service is ClusterIP only. The gateway assumes a scoped IRSA role to invoke Amazon Bedrock Mantle within the configured project scope and explicit model allowlist and to store request and response logs in a retained Amazon S3 bucket. The AWS Load Balancer Controller in kube-system watches the configured Portkey namespace and manages the load balancer. The gateway initiates a separate outbound HTTPS connection to Portkey's managed control plane for configuration and control synchronization.</desc>
  <defs>
    <marker id="arrow" markerWidth="10" markerHeight="10" refX="9" refY="5" orient="auto">
      <path d="M0,0 L10,5 L0,10 z" fill="#146EB4"/>
    </marker>
    <marker id="arrow-log" markerWidth="10" markerHeight="10" refX="9" refY="5" orient="auto">
      <path d="M0,0 L10,5 L0,10 z" fill="#248814"/>
    </marker>
    <marker id="arrow-muted" markerUnits="userSpaceOnUse"
      markerWidth="7" markerHeight="7" refX="6.5" refY="3.5" orient="auto">
      <path d="M0,0 L7,3.5 L0,7 z" fill="#8B98A5"/>
    </marker>
    <style>
      text {{ font-family: Arial, Helvetica, sans-serif; letter-spacing: 0; }}
      .request {{ fill: none; stroke: #146EB4; stroke-width: 4; stroke-linecap: round; }}
      .dependency {{
        fill: none;
        stroke: #8B98A5;
        stroke-width: 1.7;
        stroke-dasharray: 5 6;
        stroke-linecap: round;
        stroke-linejoin: round;
      }}
      .log-flow {{
        fill: none;
        stroke: #248814;
        stroke-width: 2.3;
        stroke-dasharray: 7 5;
        stroke-linecap: round;
      }}
    </style>
  </defs>

  <rect width="1600" height="980" fill="#FFFFFF"/>
  <text x="55" y="62" font-size="36" font-weight="700" fill="#161E2D">
    Codex through Portkey Hybrid on Amazon EKS
  </text>
  <text x="55" y="101" font-size="20" fill="#5F6B7A">
    Private client ingress to the customer-hosted gateway; files, tools, sandboxing, and approvals stay local
  </text>

  <rect x="35" y="145" width="300" height="640" rx="8"
    fill="#F7F8F8" stroke="#7A8793" stroke-width="2"/>
  <text x="60" y="181" font-size="18" font-weight="700" fill="#232F3E">
    Developer environment
  </text>

  <rect x="360" y="180" width="230" height="525" rx="8"
    fill="#F8FBFD" stroke="#146EB4" stroke-width="2" stroke-dasharray="8 6"/>
  {text_lines(385, 207, ["Customer-provided", "private connectivity"], size=16, color="#146EB4", weight=700, anchor="start", line_height=19)}

  <rect x="615" y="125" width="940" height="660" rx="8"
    fill="#FAFAFA" stroke="#7A8793" stroke-width="2"/>
  <text x="640" y="163" font-size="19" font-weight="700" fill="#232F3E">
    Customer AWS account
  </text>

  <rect x="640" y="180" width="585" height="555" rx="8"
    fill="#F6FAF7" stroke="#248814" stroke-width="2" stroke-dasharray="8 6"/>
  <text x="665" y="214" font-size="17" font-weight="700" fill="#248814">
    Amazon VPC
  </text>

  <rect x="890" y="185" width="305" height="525" rx="8"
    fill="#FFF9F3" stroke="#ED7100" stroke-width="2" stroke-dasharray="8 6"/>
  {image(EKS, 906, 197, 30)}
  <text x="944" y="219" font-size="17" font-weight="700" fill="#ED7100">
    Amazon EKS cluster
  </text>

  <rect x="902" y="230" width="280" height="350" rx="7"
    fill="#FFFCF8" stroke="#ED7100" stroke-width="1.5" stroke-dasharray="5 5"/>
  <text x="920" y="251" font-size="13" font-weight="700" fill="#B95700">
    Portkey namespace
  </text>

  <rect x="1245" y="180" width="285" height="555" rx="8"
    fill="#F7F8F8" stroke="#7A8793" stroke-width="1.7" stroke-dasharray="7 6"/>
  <text x="1270" y="214" font-size="16" font-weight="700" fill="#5F6B7A">
    AWS managed services
  </text>

  {developer}
  {private_access}
  {nlb}
  {gateway}
  {bedrock}
  {redis}
  {controller}
  {s3}
  {irsa}

  <rect x="65" y="500" width="240" height="155" rx="7"
    fill="#FFFFFF" stroke="#146EB4" stroke-width="2"/>
  {step_badge(91, 527, 5)}
  {text_lines(123, 521, ["Local tool execution"], size=17, color="#161E2D", weight=700, anchor="start")}
  {text_lines(88, 558, ["Codex runs approved tools", "on the workstation."], size=13, anchor="start", line_height=18)}
  {text_lines(88, 609, ["Tool results start the next", "request through steps 1–4."], size=12, anchor="start", line_height=17)}

  <rect x="385" y="500" width="180" height="130" rx="7"
    fill="#FFFFFF" stroke="#146EB4" stroke-width="1.7"/>
  {image(SslPadlock, 399, 521, 45)}
  {text_lines(456, 534, ["Not provisioned", "by this guide"], size=13, color="#161E2D", weight=700, anchor="start", line_height=17)}
  {text_lines(405, 596, ["DNS, routes, VPN, and prefix", "list remain customer-owned."], size=11, anchor="start", line_height=15)}

  <path class="request" d="M305 332 H375" marker-end="url(#arrow)"/>
  <path class="request" d="M565 332 H645" marker-end="url(#arrow)"/>
  <path class="request" d="M855 332 H905" marker-end="url(#arrow)"/>
  <path class="request" d="M1170 332 H1265" marker-end="url(#arrow)"/>
  {step_badge(340, 300, 1)}
  {step_badge(605, 300, 2)}
  {step_badge(880, 300, 3)}
  {step_badge(1218, 300, 4)}
  {text_lines(340, 371, ["HTTPS /v1/responses"], size=11, color="#146EB4", weight=700)}
  {text_lines(605, 371, ["Private route"], size=11, color="#146EB4", weight=700)}
  {text_lines(880, 371, ["TCP :8787"], size=11, color="#146EB4", weight=700)}
  {text_lines(1218, 371, ["IRSA / SigV4"], size=11, color="#146EB4", weight=700)}

  <path class="dependency" d="M1042 470 V475" marker-end="url(#arrow-muted)"/>
  <path class="dependency" d="M915 650 H875 Q845 650 845 605 V450"/>
  <path class="dependency" d="M845 450 V445" marker-end="url(#arrow-muted)"/>
  {text_lines(840, 578, ["namespace-scoped", "reconciliation"], size=11, color="#7A8793", anchor="end", line_height=15)}

  <path class="log-flow" d="M1170 445 H1205 Q1230 445 1230 470 V517 H1265"
    marker-end="url(#arrow-log)"/>
  {text_lines(1215, 480, ["logs"], size=11, color="#248814", weight=700)}
  <path class="dependency" d="M1170 435 H1190 Q1215 435 1215 460 V650 H1265"
    marker-end="url(#arrow-muted)"/>

  <rect x="835" y="815" width="695" height="84" rx="9"
    fill="#F7F8F8" stroke="#7A8793" stroke-width="2"/>
  <path d="M875 862 C875 843 890 832 906 837 C914 819 942 819 950 839
    C969 834 985 847 985 864 C985 880 973 887 956 887 H901 C885 887 875 878 875 862Z"
    fill="#FFFFFF" stroke="#7A8793" stroke-width="2"/>
  {text_lines(1010, 846, ["Portkey managed control plane"], size=17, color="#161E2D", weight=700, anchor="start")}
  {text_lines(1010, 873, ["Configuration and control synchronization · not the inference endpoint"], size=13, anchor="start")}
  <path class="dependency" d="M1150 470 H1180 V807"
    marker-end="url(#arrow-muted)"/>
  {text_lines(1095, 780, ["Data plane initiates outbound HTTPS"], size=12, color="#5F6B7A", weight=700)}

  <text x="55" y="922" font-size="15" font-weight="700" fill="#5F6B7A">REQUEST FLOW</text>
  {step_badge(185, 936, 1, color="#232F3E")}
  {text_lines(212, 931, ["Send request"], size=13, color="#161E2D", weight=700, anchor="start")}
  {text_lines(212, 951, ["Codex over HTTPS"], size=11, anchor="start")}
  {step_badge(430, 936, 2, color="#232F3E")}
  {text_lines(457, 931, ["Use private path"], size=13, color="#161E2D", weight=700, anchor="start")}
  {text_lines(457, 951, ["DNS + corporate/VPN route"], size=11, anchor="start")}
  {step_badge(745, 936, 3, color="#232F3E")}
  {text_lines(772, 931, ["Terminate TLS"], size=13, color="#161E2D", weight=700, anchor="start")}
  {text_lines(772, 951, ["Internal NLB; approved sources"], size=11, anchor="start")}
  {step_badge(1060, 936, 4, color="#232F3E")}
  {text_lines(1087, 931, ["Invoke model"], size=13, color="#161E2D", weight=700, anchor="start")}
  {text_lines(1087, 951, ["Portkey → Bedrock Mantle"], size=11, anchor="start")}
  {step_badge(1350, 936, 5, color="#232F3E")}
  {text_lines(1377, 931, ["Run tools locally"], size=13, color="#161E2D", weight=700, anchor="start")}
  {text_lines(1377, 951, ["Return results next turn"], size=11, anchor="start")}
</svg>
"""


def render_png() -> None:
    rsvg_convert = shutil.which("rsvg-convert")
    if rsvg_convert:
        subprocess.run(
            [
                rsvg_convert,
                "--width",
                "1600",
                "--height",
                "980",
                "--output",
                str(PNG_PATH),
                str(SVG_PATH),
            ],
            check=True,
        )
        return

    try:
        from cairosvg import svg2png
    except (ImportError, OSError) as error:  # pragma: no cover - environment dependent
        raise SystemExit(
            "Install librsvg (rsvg-convert) or CairoSVG to render the PNG."
        ) from error

    svg2png(
        url=str(SVG_PATH),
        write_to=str(PNG_PATH),
        output_width=1600,
        output_height=980,
    )


def main() -> None:
    SVG_PATH.write_text(build_svg(), encoding="utf-8")
    render_png()
    print(f"Wrote {SVG_PATH}")
    print(f"Wrote {PNG_PATH}")


if __name__ == "__main__":
    main()
