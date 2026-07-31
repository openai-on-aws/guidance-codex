#!/usr/bin/env python3
"""Generate the LiteLLM reference architecture with AWS service icons."""

from __future__ import annotations

import base64
import html
import subprocess
from pathlib import Path

import diagrams
from diagrams.aws.compute import ECR, Fargate
from diagrams.aws.database import RDSPostgresqlInstance
from diagrams.aws.management import Cloudwatch
from diagrams.aws.ml import Bedrock
from diagrams.aws.network import ALB
from diagrams.aws.security import KMS, SecretsManager, WAF
from diagrams.onprem.client import Users


OUTPUT_DIR = Path(__file__).resolve().parent
SVG_PATH = OUTPUT_DIR / "litellm-architecture.svg"
PNG_PATH = OUTPUT_DIR / "litellm-architecture.png"
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
) -> str:
    title_lines = [title] if isinstance(title, str) else title
    icon_size = min(76, height - 88)
    icon_x = x + (width - icon_size) // 2
    icon_y = y + 18
    title_y = icon_y + icon_size + 27
    detail_y = title_y + 27 + (len(title_lines) - 1) * 21
    return "\n".join(
        [
            f'<rect x="{x}" y="{y}" width="{width}" height="{height}" rx="7" '
            f'fill="{fill}" stroke="{border}" stroke-width="2"/>',
            image(icon_class, icon_x, icon_y, icon_size),
            text_lines(
                x + width // 2,
                title_y,
                title_lines,
                size=18,
                color="#161E2D",
                weight=700,
                line_height=21,
            ),
            text_lines(
                x + width // 2,
                detail_y,
                detail,
                size=15,
                line_height=20,
            ),
        ]
    )


def support_card(
    x: int,
    y: int,
    width: int,
    icon_classes: list[type],
    title: str,
    detail: str,
) -> str:
    parts = [
        f'<rect x="{x}" y="{y}" width="{width}" height="92" rx="7" '
        'fill="#FFFFFF" stroke="#AAB7B8" stroke-width="2"/>'
    ]
    icon_size = 46
    for index, icon_class in enumerate(icon_classes):
        parts.append(image(icon_class, x + 18 + index * 48, y + 22, icon_size))
    text_x = x + 18 + len(icon_classes) * 48 + 10
    parts.extend(
        [
            text_lines(
                text_x,
                y + 36,
                [title],
                size=16,
                color="#161E2D",
                weight=700,
                anchor="start",
            ),
            text_lines(
                text_x,
                y + 61,
                [detail],
                size=13,
                anchor="start",
            ),
        ]
    )
    return "\n".join(parts)


def step_badge(x: int, y: int, number: int, *, color: str = "#146EB4") -> str:
    return (
        f'<circle cx="{x}" cy="{y}" r="18" fill="{color}"/>'
        f'<text x="{x}" y="{y + 6}" text-anchor="middle" font-size="17" '
        f'font-weight="700" fill="#FFFFFF">{number}</text>'
    )


def legend_item(x: int, number: int, title: str, detail: str) -> str:
    return "\n".join(
        [
            step_badge(x, 885, number, color="#232F3E"),
            text_lines(
                x + 30,
                878,
                [title],
                size=15,
                color="#161E2D",
                weight=700,
                anchor="start",
            ),
            text_lines(
                x + 30,
                901,
                [detail],
                size=13,
                anchor="start",
            ),
        ]
    )


def build_svg() -> str:
    cards = [
        card(
            92,
            235,
            220,
            210,
            Users,
            "Developer workstation",
            ["Codex CLI", "Local files and tools"],
            border="#146EB4",
            fill="#F3FAFD",
        ),
        card(
            430,
            235,
            150,
            190,
            WAF,
            "AWS WAF",
            ["Web protection"],
            border="#8C4FFF",
            fill="#FBF8FF",
        ),
        card(
            675,
            245,
            175,
            195,
            ALB,
            ["Application Load", "Balancer"],
            ["Public subnets", "TLS and routing"],
            border="#8C4FFF",
            fill="#FBF8FF",
        ),
        card(
            930,
            245,
            190,
            195,
            Fargate,
            "LiteLLM on Fargate",
            ["Private app subnets", "Policy and routing"],
            border="#ED7100",
            fill="#FFF9F3",
        ),
        card(
            1275,
            235,
            215,
            210,
            Bedrock,
            "Amazon Bedrock",
            ["Approved model", "Responses API"],
            border="#01A88D",
            fill="#F2FCFA",
        ),
    ]

    supports = [
        support_card(
            430,
            700,
            330,
            [SecretsManager, KMS],
            "Secrets Manager + KMS",
            "Encrypted keys and credentials",
        ),
        support_card(
            785,
            700,
            210,
            [ECR],
            "Amazon ECR",
            "Digest-pinned image",
        ),
        support_card(
            1020,
            700,
            260,
            [Cloudwatch],
            "Amazon CloudWatch",
            "Logs, metrics, and alarms",
        ),
    ]

    return f"""<svg xmlns="http://www.w3.org/2000/svg"
  xmlns:xlink="http://www.w3.org/1999/xlink"
  width="1600" height="960" viewBox="0 0 1600 960"
  role="img" aria-labelledby="title desc">
  <title id="title">Codex through a customer-operated LiteLLM gateway on AWS</title>
  <desc id="desc">Codex sends Responses API requests through AWS WAF and an Application Load Balancer to LiteLLM on Amazon ECS Fargate. LiteLLM applies identity and consumption policy before invoking an approved model on Amazon Bedrock. Amazon RDS for PostgreSQL, Secrets Manager, KMS, ECR, and CloudWatch support the gateway.</desc>
  <defs>
    <marker id="arrow" markerWidth="10" markerHeight="10" refX="9" refY="5" orient="auto">
      <path d="M0,0 L10,5 L0,10 z" fill="#146EB4"/>
    </marker>
    <marker id="arrow-muted" markerUnits="userSpaceOnUse"
      markerWidth="8" markerHeight="8" refX="7" refY="4" orient="auto">
      <path d="M0,0 L8,4 L0,8 z" fill="#98A4AE"/>
    </marker>
    <style>
      text {{ font-family: Arial, Helvetica, sans-serif; letter-spacing: 0; }}
    </style>
  </defs>

  <rect width="1600" height="900" fill="#FFFFFF"/>
  <text x="55" y="62" font-size="36" font-weight="700" fill="#161E2D">
    Codex through a customer-operated LiteLLM gateway on AWS
  </text>
  <text x="55" y="101" font-size="20" fill="#5F6B7A">
    Central policy for model turns; local files, tools, sandboxing, and approvals remain on the developer workstation
  </text>

  <rect x="45" y="155" width="300" height="470" rx="8"
    fill="#F7F8F8" stroke="#7A8793" stroke-width="2"/>
  <text x="68" y="190" font-size="18" font-weight="700" fill="#232F3E">
    Developer environment
  </text>

  <rect x="385" y="125" width="1165" height="710" rx="8"
    fill="#FAFAFA" stroke="#7A8793" stroke-width="2"/>
  <text x="410" y="163" font-size="19" font-weight="700" fill="#232F3E">
    Customer AWS account
  </text>

  <rect x="625" y="188" width="580" height="457" rx="8"
    fill="#F6FAF7" stroke="#248814" stroke-width="2" stroke-dasharray="8 6"/>
  <text x="650" y="220" font-size="17" font-weight="700" fill="#248814">
    Amazon VPC
  </text>

  {"".join(cards)}

  <rect x="865" y="485" width="255" height="112" rx="7"
    fill="#FFFFFF" stroke="#2E73B8" stroke-width="2"/>
  {image(RDSPostgresqlInstance, 883, 516, 50)}
  {text_lines(950, 513, ["Amazon RDS for", "PostgreSQL"], size=15, color="#161E2D", weight=700, anchor="start", line_height=19)}
  {text_lines(950, 559, ["Private DB subnets"], size=13, anchor="start")}
  {text_lines(950, 581, ["State, usage, and budgets"], size=12, anchor="start")}

  <rect x="79" y="480" width="246" height="118" rx="7"
    fill="#FFFFFF" stroke="#146EB4" stroke-width="2"/>
  {step_badge(105, 507, 5, color="#146EB4")}
  {text_lines(137, 502, ["Local tool execution"], size=17, color="#161E2D", weight=700, anchor="start")}
  {text_lines(103, 538, ["Codex runs approved commands", "on the workstation."], size=13, anchor="start", line_height=18)}
  {text_lines(103, 579, ["The next model turn repeats steps 1-4."], size=13, anchor="start")}

  <path d="M312 340 H420" fill="none" stroke="#146EB4" stroke-width="4" marker-end="url(#arrow)"/>
  <path d="M580 340 H665" fill="none" stroke="#146EB4" stroke-width="4" marker-end="url(#arrow)"/>
  <path d="M850 340 H920" fill="none" stroke="#146EB4" stroke-width="4" marker-end="url(#arrow)"/>
  <path d="M1120 340 H1265" fill="none" stroke="#146EB4" stroke-width="4" marker-end="url(#arrow)"/>
  {step_badge(365, 308, 1)}
  {step_badge(623, 308, 2)}
  {step_badge(885, 308, 3)}
  {step_badge(1192, 308, 4)}

  <path d="M1025 440 V490" fill="none" stroke="#98A4AE" stroke-width="2"
    stroke-dasharray="6 6" marker-end="url(#arrow-muted)"/>

  <text x="430" y="674" font-size="14" font-weight="700" fill="#7A8793">
    GATEWAY DEPENDENCIES
  </text>
  <path d="M1120 425 H1208 Q1230 425 1230 447 V648 Q1230 670 1208 670 H595"
    fill="none" stroke="#98A4AE" stroke-width="2" stroke-dasharray="6 6"/>
  <path d="M595 670 V692" fill="none" stroke="#98A4AE" stroke-width="2"
    marker-end="url(#arrow-muted)"/>
  <path d="M890 670 V692" fill="none" stroke="#98A4AE" stroke-width="2"
    marker-end="url(#arrow-muted)"/>
  <path d="M1150 670 V692" fill="none" stroke="#98A4AE" stroke-width="2"
    marker-end="url(#arrow-muted)"/>

  {"".join(supports)}

  <text x="55" y="862" font-size="16" font-weight="700" fill="#5F6B7A">REQUEST FLOW</text>
  {legend_item(85, 1, "Send request", "HTTPS /v1/responses")}
  {legend_item(360, 2, "Protect ingress", "Inspect and route")}
  {legend_item(650, 3, "Apply policy", "Identity, model, budget, rate")}
  {legend_item(990, 4, "Invoke model", "Amazon Bedrock request/response")}
  {legend_item(1325, 5, "Run tools locally", "Return results on the next turn")}
</svg>
"""


def main() -> None:
    SVG_PATH.write_text(build_svg(), encoding="utf-8")
    subprocess.run(
        [
            "rsvg-convert",
            "--width",
            "1600",
            "--height",
            "960",
            "--output",
            str(PNG_PATH),
            str(SVG_PATH),
        ],
        check=True,
    )
    print(f"Wrote {SVG_PATH}")
    print(f"Wrote {PNG_PATH}")


if __name__ == "__main__":
    main()
