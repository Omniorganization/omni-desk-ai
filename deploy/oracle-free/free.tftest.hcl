mock_provider "oci" {
  # Resolve computed set members without overriding any configured policy value.
  override_during = plan
}

variables {
  home_region                = "ap-singapore-1"
  selected_region            = "ap-singapore-1"
  compartment_id             = "ocid1.compartment.oc1..testfixture"
  availability_domain        = "fixture:AP-SINGAPORE-1-AD-1"
  image_id                   = "ocid1.image.oc1.ap-singapore-1.testfixture"
  image_verified_always_free = true
  quoted_monthly_usd         = 0
  free_capacity              = { ocpu_hours = 1500, ram_gb_hours = 9000, boot_disk_gb = 200 }
  ssh_public_key             = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIK7txPvTBVoEV4RJwdMHngFx2JJsXeUgDoqhcaWMjGFN test-only"
  ssh_cidrs                  = ["192.0.2.17/32"]
}
run "fixed_free_single_host" {
  command = plan
  assert {
    condition     = oci_core_instance.acceptance.shape == "VM.Standard.A1.Flex" && oci_core_instance.acceptance.shape_config[0].ocpus == 2 && oci_core_instance.acceptance.shape_config[0].memory_in_gbs == 12
    error_message = "No paid shape or extra CPU/RAM is permitted."
  }
  assert {
    condition     = tonumber(oci_core_instance.acceptance.source_details[0].boot_volume_size_in_gbs) == 50 && tonumber(oci_core_instance.acceptance.source_details[0].boot_volume_vpus_per_gb) == 10 && oci_core_instance.acceptance.preserve_boot_volume
    error_message = "No paid shape, extra CPU/RAM/disk or destructive volume cleanup is permitted."
  }
  assert {
    condition     = length(oci_core_security_list.acceptance.ingress_security_rules) == 4 && alltrue([for rule in oci_core_security_list.acceptance.ingress_security_rules : rule.protocol != "6" || try(contains([22, 80, 443], rule.tcp_options[0].min) && rule.tcp_options[0].min == rule.tcp_options[0].max, false)])
    error_message = "Database, gateway and sandbox ports must stay private."
  }
  assert {
    condition     = alltrue([for rule in oci_core_security_list.acceptance.ingress_security_rules : rule.source_type == "CIDR_BLOCK" && !rule.stateless && (rule.protocol == "6" ? try(rule.tcp_options[0].min == 22 ? contains(var.ssh_cidrs, rule.source) : rule.source == "0.0.0.0/0", false) : try(rule.protocol == "1" && rule.source == "0.0.0.0/0" && rule.icmp_options[0].type == 3 && rule.icmp_options[0].code == 4, false))])
    error_message = "Only stateful web, IP-restricted operator SSH and IPv4 fragmentation-needed ICMP may be exposed."
  }
  assert {
    condition     = length([for rule in oci_core_security_list.acceptance.ingress_security_rules : rule if rule.protocol == "1"]) == 1
    error_message = "Path MTU discovery must retain exactly one ICMP destination-unreachable/fragmentation-needed ingress rule."
  }
}
run "reject_missing_ed25519_wire_key" {
  command = plan
  variables {
    ssh_public_key = "ssh-ed25519 AAAA"
  }
  expect_failures = [var.ssh_public_key]
}
run "reject_wrong_ed25519_wire_key_length" {
  command = plan
  variables {
    ssh_public_key = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIA=="
  }
  expect_failures = [var.ssh_public_key]
}
run "reject_paid_quote" {
  command = plan
  variables {
    quoted_monthly_usd = 0.01
  }
  expect_failures = [var.quoted_monthly_usd]
}
run "reject_exhausted_account_allowance" {
  command = plan
  variables {
    free_capacity = { ocpu_hours = 1400, ram_gb_hours = 9000, boot_disk_gb = 200 }
  }
  expect_failures = [var.free_capacity]
}
run "reject_public_ssh" {
  command = plan
  variables {
    ssh_cidrs = ["0.0.0.0/0"]
  }
  expect_failures = [var.ssh_cidrs]
}
run "reject_ipv6_network_as_operator_host" {
  command = plan
  variables {
    ssh_cidrs = ["2001:db8::/32"]
  }
  expect_failures = [var.ssh_cidrs]
}
run "reject_nonfree_image" {
  command = plan
  variables {
    image_verified_always_free = false
  }
  expect_failures = [var.image_verified_always_free]
}
run "reject_non_home_region" {
  command = plan
  variables {
    selected_region = "us-phoenix-1"
  }
  expect_failures = [oci_core_instance.acceptance]
}
