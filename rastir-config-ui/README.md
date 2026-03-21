# Rastir Config UI

Runtime configuration UI for Rastir server.

## Features

- View current server configuration
- Update sampling rate
- Enable/disable evaluation
- Configure rate limits
- Adjust backpressure settings
- Modify logging level
- Update SRE budgets

## Usage

### Local Development

```bash
cd deploy/docker
docker compose up -d
```

Access the UI at: http://localhost:8081

### Kubernetes

The Config UI is designed to run as a sidecar container in the same pod as the Rastir server.

## Configuration

The UI connects to the Rastir server at `http://rastir-server:8080` by default.

Runtime configuration is stored in `/etc/rastir/runtime-config.yaml` and has the following precedence:

```
defaults < base_yaml < env_vars < runtime_overrides
```

## API Endpoints

The Config UI uses the following server endpoints:

- `GET /config` - Get current configuration
- `PUT /config` - Update runtime configuration
- `POST /config/reload` - Reload configuration from file
