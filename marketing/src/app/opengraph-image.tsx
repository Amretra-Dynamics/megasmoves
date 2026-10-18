import { ImageResponse } from "next/og";

export const dynamic = "force-static";
export const size = { width: 1200, height: 630 };
export const contentType = "image/png";

export default function OpenGraphImage() {
  return new ImageResponse(
    (
      <div
        style={{
          background: "linear-gradient(125deg, #081624 0%, #0b2336 55%, #123750 100%)",
          color: "#effcff",
          width: "100%",
          height: "100%",
          display: "flex",
          flexDirection: "column",
          justifyContent: "space-between",
          padding: "62px 72px",
          fontFamily: "sans-serif",
        }}
      >
        <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>
          <div style={{ display: "flex", alignItems: "center", gap: 16 }}>
            <div style={{ width: 18, height: 18, borderRadius: 6, background: "#5ae4d0" }} />
            <span style={{ color: "#83dbc9", letterSpacing: 5, fontSize: 23, fontWeight: 700 }}>
              AMRETRA DYNAMICS
            </span>
          </div>
          <div style={{ fontSize: 100, fontWeight: 800, letterSpacing: -6, marginTop: 46 }}>MEGASMOVES</div>
          <div style={{ fontSize: 44, fontWeight: 600, color: "#b8edf3" }}>
            Your SI agent. Across your devices.
          </div>
        </div>
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
          <span style={{ fontSize: 27, color: "#c1d7e3" }}>Local tools  /  MCP  /  Native automation</span>
          <span style={{ color: "#5ae4d0", fontSize: 25, fontWeight: 700 }}>megasmoves.io</span>
        </div>
      </div>
    ),
    size
  );
}
