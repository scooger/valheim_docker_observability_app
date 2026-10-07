# docker-lite

Hardware inventory captured 2026-10-07 from the live host (`192.168.10.12`).

| | |
| --- | --- |
| Hostname | `docker-lite` |
| Address | `192.168.10.12/24` on `enp0s31f6` |
| OS | Ubuntu 26.04.1 LTS |
| Kernel | Linux 7.0.0-38-generic |
| Chassis | Desktop |
| Board | ASRock Z370 Killer SLI/ac |
| BIOS | P1.50 (2018-02-22) |

## CPU

Intel Core i7-8700K (Coffee Lake, LGA 1151).

| | |
| --- | --- |
| Cores / threads | 6 / 12 |
| Base / max | 3.70 GHz / 4.70 GHz |
| Cache | L1 192 KiB + 192 KiB, L2 1.5 MiB, L3 12 MiB |
| Virtualization | VT-x |

## Memory

16 GB DDR4 installed as 2×8 GB Corsair `CMK16GX4M2B3200C16`. The board has four DIMM slots and a 64 GB maximum. Both sticks are in channel B (`ChannelB-DIMM0` and `ChannelB-DIMM1`); channel A is empty. Configured speed is 2133 MT/s. The OS sees about 14 GiB. Swap is 4 GiB and was unused at capture.

## Graphics

Intel UHD Graphics 630 (Coffee Lake GT2), integrated. No discrete GPU.

## Storage

| Device | Model | Size | Use |
| --- | --- | --- | --- |
| `nvme0n1` | ADATA SX8000NP | 477 GB NVMe | Boot disk |
| `sda` | Toshiba HDWD130 | 2.7 TB SATA HDD | Data disk, label `Hoarder` |

Boot disk layout:

| Partition | Size | Filesystem | Mount |
| --- | --- | --- | --- |
| `nvme0n1p1` | 1 MB | — | — |
| `nvme0n1p2` | 2 GB | ext4 | `/boot` |
| `nvme0n1p3` | 475 GB | LVM (`ubuntu-vg`) | — |
| `ubuntu-lv` | 100 GB | ext4 | `/` |

The root volume is 100 GB of the 475 GB LVM physical volume. At capture, `/` was 14 GB used of 98 GB.

The Toshiba disk has a 128 MB partition and a 2.7 TB NTFS partition labeled `Hoarder`. That partition was not mounted.
