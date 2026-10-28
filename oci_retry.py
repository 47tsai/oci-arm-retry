import oci
import time
import datetime
import json
import os
import urllib.request
import urllib.error

# ╔══════════════════════════════════════════════════════╗
# ║  Target : VM.Standard.A1.Flex (ARM)                  ║
# ║  Spec   : 2 OCPU / 12 GB RAM / 100 GB disk (default) ║
# ║  Arch   : ARM (Ampere)                               ║
# ║  Tier   : Oracle Always Free                         ║
# ║  OS     : Canonical Ubuntu 22.04                     ║
# ╚══════════════════════════════════════════════════════╝

# ─── Configuration ───────────────────────────────────────
COMPARTMENT_ID = (
    "ocid1.tenancy.oc1..aaaaaaaahds24vzvh5xqa6ysnubzv5ljdkkuqeonqzz4quro5dpjwntyk4va"  # Replace with your tenancy OCID
)
SSH_PUBLIC_KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIJzw6MlJrOu3+DFqesQz1a5l9+6V3c/yd9kfEJJ6yfqh oci-a1-retry"  # Dedicated public key for the new ARM instance
INSTANCE_NAME = "streamlit-server"
ARM_OCPUS = 2
ARM_MEMORY_IN_GBS = 12
BOOT_VOLUME_SIZE_IN_GBS = 100
RETRY_INTERVAL = 90  # seconds
# ────────────────────────────────────────────────────────

# ─── OCI Authentication ──────────────────────────────────
# [Local] Create a config file at ~/.oci/config
#   See README.md for format; key_file should point to your API private key .pem
#
# [GitHub Actions] No config file needed
#   The workflow auto-creates it from GitHub Secrets — see README.md
# ────────────────────────────────────────────────────────
config = oci.config.from_file()


def get_availability_domain():
    identity = oci.identity.IdentityClient(config)
    ads = identity.list_availability_domains(COMPARTMENT_ID).data
    return ads[0].name


def get_ubuntu_arm_image():
    compute = oci.core.ComputeClient(config)
    images = compute.list_images(
        COMPARTMENT_ID,
        operating_system="Canonical Ubuntu",
        operating_system_version="22.04",
        shape="VM.Standard.A1.Flex",
        sort_by="TIMECREATED",
        sort_order="DESC",
    ).data
    if not images:
        raise Exception("Ubuntu 22.04 ARM image not found")
    return images[0].id


def create_vcn_and_subnet():
    network = oci.core.VirtualNetworkClient(config)

    # Prefer the dedicated VCN if it exists.
    named_vcns = network.list_vcns(COMPARTMENT_ID, display_name="retry-vcn").data
    if named_vcns:
        vcn = named_vcns[0]
        print(f"Using existing VCN: {vcn.id}")
    else:
        # VCN quotas can be very low on Free Tier. Reuse an existing public
        # subnet rather than trying to create another VCN unnecessarily.
        existing_vcns = network.list_vcns(COMPARTMENT_ID).data
        for existing_vcn in existing_vcns:
            existing_subnets = network.list_subnets(
                COMPARTMENT_ID, vcn_id=existing_vcn.id
            ).data
            public_subnets = [
                subnet for subnet in existing_subnets
                if subnet.lifecycle_state == "AVAILABLE"
                and not subnet.prohibit_public_ip_on_vnic
            ]
            if public_subnets:
                subnet = public_subnets[0]
                print(
                    f"Reusing existing VCN: {existing_vcn.id}; "
                    f"public subnet: {subnet.id}"
                )
                return subnet.id

        if existing_vcns:
            raise RuntimeError(
                "VCN limit reached and no existing subnet allowing public IPs "
                "was found in this compartment. In OCI Console, check the "
                "existing VCN/subnet and its internet gateway/route/security "
                "rules, or free up an unused VCN."
            )

        vcn = network.create_vcn(
            oci.core.models.CreateVcnDetails(
                compartment_id=COMPARTMENT_ID,
                display_name="retry-vcn",
                cidr_block="10.0.0.0/16",
            )
        ).data
        print(f"Created VCN: {vcn.id}")

        # Create Internet Gateway
        ig = network.create_internet_gateway(
            oci.core.models.CreateInternetGatewayDetails(
                compartment_id=COMPARTMENT_ID,
                vcn_id=vcn.id,
                display_name="retry-ig",
                is_enabled=True,
            )
        ).data

        # Update route table to allow outbound traffic
        network.update_route_table(
            vcn.default_route_table_id,
            oci.core.models.UpdateRouteTableDetails(
                route_rules=[
                    oci.core.models.RouteRule(
                        destination="0.0.0.0/0",
                        network_entity_id=ig.id,
                    )
                ]
            ),
        )

        # Open inbound ports: SSH / HTTP / HTTPS / Streamlit
        security_lists = network.list_security_lists(COMPARTMENT_ID, vcn_id=vcn.id).data
        if security_lists:
            existing_egress = security_lists[0].egress_security_rules
            new_ingress = []
            for port in [22, 80, 443, 8501]:
                new_ingress.append(
                    oci.core.models.IngressSecurityRule(
                        protocol="6",
                        source="0.0.0.0/0",
                        tcp_options=oci.core.models.TcpOptions(
                            destination_port_range=oci.core.models.PortRange(
                                min=port, max=port
                            )
                        ),
                    )
                )
            network.update_security_list(
                security_lists[0].id,
                oci.core.models.UpdateSecurityListDetails(
                    ingress_security_rules=new_ingress,
                    egress_security_rules=existing_egress,
                ),
            )

    # Check if subnet already exists
    subnets = network.list_subnets(
        COMPARTMENT_ID, vcn_id=vcn.id, display_name="retry-subnet"
    ).data
    if subnets:
        subnet = subnets[0]
        print(f"Using existing subnet: {subnet.id}")
    else:
        subnet = network.create_subnet(
            oci.core.models.CreateSubnetDetails(
                compartment_id=COMPARTMENT_ID,
                vcn_id=vcn.id,
                display_name="retry-subnet",
                cidr_block="10.0.0.0/24",
                prohibit_public_ip_on_vnic=False,
            )
        ).data
        print(f"Created subnet: {subnet.id}")

    return subnet.id


def try_create_instance(subnet_id, ad_name, image_id):
    compute = oci.core.ComputeClient(config)
    instance = compute.launch_instance(
        oci.core.models.LaunchInstanceDetails(
            compartment_id=COMPARTMENT_ID,
            display_name=INSTANCE_NAME,
            availability_domain=ad_name,
            shape="VM.Standard.A1.Flex",
            shape_config=oci.core.models.LaunchInstanceShapeConfigDetails(
                ocpus=ARM_OCPUS,
                memory_in_gbs=ARM_MEMORY_IN_GBS,
            ),
            source_details=oci.core.models.InstanceSourceViaImageDetails(
                image_id=image_id,
                boot_volume_size_in_gbs=BOOT_VOLUME_SIZE_IN_GBS,
            ),
            create_vnic_details=oci.core.models.CreateVnicDetails(
                subnet_id=subnet_id,
                assign_public_ip=True,
            ),
            metadata={"ssh_authorized_keys": SSH_PUBLIC_KEY},
        )
    ).data
    return instance


def verify_instance(instance_id, timeout_seconds=300):
    """Wait for RUNNING state and discover the public IP, without launching again."""
    compute = oci.core.ComputeClient(config)
    network = oci.core.VirtualNetworkClient(config)
    deadline = time.time() + timeout_seconds
    last_state = "UNKNOWN"
    public_ip = None

    while time.time() < deadline:
        instance = compute.get_instance(instance_id).data
        last_state = instance.lifecycle_state
        print(f"Validation: instance state = {last_state}")

        if last_state in ("TERMINATED", "TERMINATING"):
            raise RuntimeError(
                f"Instance {instance_id} entered unexpected state {last_state}"
            )

        attachments = compute.list_vnic_attachments(
            COMPARTMENT_ID, instance_id=instance_id
        ).data
        for attachment in attachments:
            if attachment.lifecycle_state != "ATTACHED":
                continue
            vnic = network.get_vnic(attachment.vnic_id).data
            if vnic.public_ip:
                public_ip = vnic.public_ip
                break

        if last_state == "RUNNING" and public_ip:
            return instance, public_ip
        time.sleep(15)

    print(
        f"⚠️ Validation timed out after {timeout_seconds}s: "
        f"state={last_state}, public_ip={public_ip or 'not assigned'}"
    )
    return compute.get_instance(instance_id).data, public_ip


def send_discord_success(instance, public_ip):
    """Send one success notification when the optional webhook secret is configured."""
    webhook_url = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
    if not webhook_url:
        print("Discord notification skipped: DISCORD_WEBHOOK_URL secret is not set.")
        return

    content = (
        "✅ **Oracle Cloud ARM A1.Flex 建立成功**\n"
        f"名稱：{instance.display_name}\n"
        f"狀態：{instance.lifecycle_state}\n"
        f"Instance OCID：{instance.id}\n"
        f"公網 IP：{public_ip or '尚未取得，請到 OCI Console 確認'}"
    )
    payload = json.dumps({"content": content}).encode("utf-8")
    request = urllib.request.Request(
        webhook_url,
        data=payload,
        headers={"Content-Type": "application/json", "User-Agent": "oci-arm-retry"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            print(f"Discord notification sent (HTTP {response.status}).")
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        # A notification failure must not turn a successfully created VM into a retry.
        print(f"⚠️ Discord notification failed: {type(exc).__name__}: {exc}")


def main():
    print("Initializing network configuration...")
    subnet_id = create_vcn_and_subnet()

    print("Fetching availability domain...")
    ad_name = get_availability_domain()
    print(f"AD: {ad_name}")

    print("Fetching Ubuntu 22.04 ARM image...")
    image_id = get_ubuntu_arm_image()
    print(f"Image ID: {image_id}")

    attempt = 0
    while True:
        attempt += 1
        now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        print(f"\n[{now}] Attempt #{attempt} to create instance...")

        instance = None
        try:
            instance = try_create_instance(subnet_id, ad_name, image_id)
            print(f"\n✅ Instance creation request accepted")
            print(f"   ID: {instance.id}")
        except oci.exceptions.ServiceError as e:
            if "Out of host capacity" in str(e) or "capacity" in str(e).lower():
                print("❌ Out of host capacity; retrying...")
            elif e.status == 429 or e.code == "TooManyRequests":
                # OCI rate-limits launch requests occasionally. Back off longer
                # instead of terminating the workflow and losing the retry loop.
                backoff = max(RETRY_INTERVAL, 300)
                print(
                    f"⚠️ OCI rate limit (429 TooManyRequests). "
                    f"Waiting {backoff}s before retrying..."
                )
                time.sleep(backoff)
                continue
            elif e.status in (408, 500, 502, 503, 504) or e.status >= 500:
                # Transient OCI service failures should not break the retry chain.
                print(
                    f"⚠️ Temporary OCI service error "
                    f"(HTTP {e.status}, {e.code}): {e.message}. "
                    f"Retrying in {RETRY_INTERVAL}s..."
                )
            elif e.status in (401, 403) or e.code in (
                "NotAuthorized", "NotAuthorizedOrNotFound"
            ):
                print(
                    f"🛑 OCI authentication/permission error "
                    f"(HTTP {e.status}, {e.code}): {e.message}"
                )
                raise
            elif e.code in ("LimitExceeded", "QuotaExceeded"):
                print(
                    f"🛑 OCI quota/limit error ({e.code}): {e.message}. "
                    "This will not be fixed by repeated retries."
                )
                raise
            else:
                print(
                    f"🛑 Non-retryable OCI API error "
                    f"(HTTP {e.status}, {e.code}): {e.message}"
                )
                raise
        except Exception as e:
            print(f"⚠️ Network timeout or other error, retrying... ({type(e).__name__})")

        if instance is not None:
            # Do not return to the launch loop after OCI has created the instance.
            instance, public_ip = verify_instance(instance.id)
            print(f"   Final state: {instance.lifecycle_state}")
            print(f"   Public IP: {public_ip or 'not assigned yet'}")
            send_discord_success(instance, public_ip)
            break

        time.sleep(RETRY_INTERVAL)


if __name__ == "__main__":
    main()
