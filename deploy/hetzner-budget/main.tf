terraform {
  required_version = "= 1.16.4"
  required_providers {
    hcloud = {
      source  = "hetznercloud/hcloud"
      version = "= 1.69.0"
    }
  }
}

# Credentials come exclusively from HCLOUD_TOKEN, never from tfvars or user_data.
provider "hcloud" {}

variable "name" {
  type    = string
  default = "omnidesk-acceptance"
  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{2,40}$", var.name))
    error_message = "Use a short lowercase resource name."
  }
}

variable "location" {
  type    = string
  default = "nbg1"
  validation {
    condition     = contains(["nbg1", "hel1"], var.location)
    error_message = "Only the quoted European CX23 locations are allowed; no paid fallback."
  }
}

variable "ssh_public_key" {
  type = string
  validation {
    condition     = can(regex("^ssh-ed25519 [A-Za-z0-9+/]+={0,3}( .*)?$", var.ssh_public_key))
    error_message = "Provide a dedicated deployment public key, never its private key."
  }
}

variable "ssh_cidrs" {
  type = list(string)
  validation {
    condition = length(var.ssh_cidrs) > 0 && alltrue([
      for cidr in var.ssh_cidrs : can(cidrhost(cidr, 0)) && can(regex("(/32|/128)$", cidr))
    ])
    error_message = "SSH requires explicit client IPv4 /32 or IPv6 /128 addresses."
  }
}

variable "quoted_total_usd" {
  description = "Console-verified MONTHLY GROSS total: CX23, IPv4, backups, tax and currency conversion. This is a quote gate, not a billing cap."
  type        = number
  validation {
    condition     = var.quoted_total_usd > 0 && var.quoted_total_usd <= 10
    error_message = "The complete monthly quote must be positive and no more than the authorized USD 10."
  }
}

resource "hcloud_ssh_key" "deployer" {
  name       = "${var.name}-deployer"
  public_key = var.ssh_public_key
}

resource "hcloud_firewall" "acceptance" {
  name = "${var.name}-ingress"
  rule {
    direction  = "in"
    protocol   = "tcp"
    port       = "22"
    source_ips = var.ssh_cidrs
  }
  rule {
    direction  = "in"
    protocol   = "tcp"
    port       = "80"
    source_ips = ["0.0.0.0/0", "::/0"]
  }
  rule {
    direction  = "in"
    protocol   = "tcp"
    port       = "443"
    source_ips = ["0.0.0.0/0", "::/0"]
  }
}

resource "hcloud_server" "acceptance" {
  name               = var.name
  server_type        = "cx23"
  image              = "ubuntu-24.04"
  location           = var.location
  ssh_keys           = [hcloud_ssh_key.deployer.id]
  firewall_ids       = [hcloud_firewall.acceptance.id]
  backups            = true
  delete_protection  = true
  rebuild_protection = true
  public_net {
    ipv4_enabled = true
    ipv6_enabled = true
  }
  labels = {
    project = "omnidesk"
    purpose = "single-node-acceptance"
  }
  user_data = file("${path.module}/cloud-init.yaml")
  lifecycle {
    prevent_destroy = true
  }
}

output "acceptance_ipv4" {
  value = hcloud_server.acceptance.ipv4_address
}

output "scope" {
  value = "Persistent single host only. No application deployment, HA, production signature or GA evidence is implied."
}
