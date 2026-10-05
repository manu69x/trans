FROM node:20-alpine

ENV NODE_ENV=production \
    PNP_DISABLE_VIRTUAIZATION=1 \
    DISABLE_ESLINT_PLUGIN=true \
    NEXT_TELEMETRY_DISABLED=1

WORKDIR /app

COPY package.json package-lock.json* ./
# Install first to leverage layer caching.
RUN npm install --omit=dev --no-audit --no-fund || npm ci --omit=dev --no-audit --no-fund

COPY . .

# Production build; output is served by `next start`.
RUN npm run build

# Compose wires NEXT_PUBLIC_API_BASE and NODE_ENV.
EXPOSE 3000

CMD ["npm", "run", "start"]
