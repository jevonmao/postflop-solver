import type { Config } from 'tailwindcss';

const config: Config = {
  content: [
    './app/**/*.{ts,tsx}',
    './components/**/*.{ts,tsx}',
  ],
  theme: {
    extend: {
      colors: {
        oop: '#2563eb',
        ip:  '#dc2626',
        nut: '#16a34a',
      },
    },
  },
  plugins: [],
};

export default config;
