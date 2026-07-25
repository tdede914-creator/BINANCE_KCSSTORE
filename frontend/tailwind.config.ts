import type { Config } from "tailwindcss";

const config: Config = {
  content: [
    "./app/**/*.{ts,tsx}",
    "./components/**/*.{ts,tsx}",
    "./lib/**/*.{ts,tsx}",
  ],
  theme: {
    extend: {
      colors: {
        bg: {
          DEFAULT: "#0b0f1a",
          soft: "#131a2b",
          card: "#1a2238",
        },
        border: {
          DEFAULT: "#26304a",
        },
        long: "#2ecc71",
        short: "#e74c3c",
        muted: "#8892b0",
      },
      fontFamily: {
        mono: ["ui-monospace", "SFMono-Regular", "Menlo", "monospace"],
      },
    },
  },
  plugins: [],
};

export default config;
