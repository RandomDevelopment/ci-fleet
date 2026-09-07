package main

import (
	"context"
	"fmt"
	"net/netip"
	"sort"

	"github.com/docker/docker/api/types/network"
)

type slotInterval struct {
	start uint64
	end   uint64
}

type addressPool struct {
	prefix netip.Prefix
	size   int
}

func (s *Scaler) availableNetworkRunnerSlots(ctx context.Context) (int, error) {
	info, err := s.dockerClient.Info(ctx)
	if err != nil {
		return 0, fmt.Errorf("inspect Docker address pools: %w", err)
	}
	pools := make([]addressPool, 0, len(info.DefaultAddressPools))
	configured := uint64(0)
	for _, value := range info.DefaultAddressPools {
		prefix, err := netip.ParsePrefix(value.Base)
		if err != nil || !prefix.Addr().Is4() || prefix != prefix.Masked() || value.Size < prefix.Bits() || value.Size > 29 {
			return 0, fmt.Errorf("Docker reported an invalid default address pool")
		}
		for _, existing := range pools {
			if prefix.Overlaps(existing.prefix) {
				return 0, fmt.Errorf("Docker reported overlapping default address pools")
			}
		}
		pools = append(pools, addressPool{prefix: prefix, size: value.Size})
		configured += uint64(1) << (value.Size - prefix.Bits())
	}
	if len(pools) == 0 {
		return 0, fmt.Errorf("Docker reported no default address pools")
	}

	networks, err := s.dockerClient.NetworkList(ctx, network.ListOptions{})
	if err != nil {
		return 0, fmt.Errorf("list Docker networks: %w", err)
	}
	occupied := make([][]slotInterval, len(pools))
	for _, item := range networks {
		for _, value := range item.IPAM.Config {
			if value.Subnet == "" {
				continue
			}
			subnet, err := netip.ParsePrefix(value.Subnet)
			if err != nil {
				return 0, fmt.Errorf("Docker network %q reported an invalid subnet", item.Name)
			}
			if !subnet.Addr().Is4() {
				continue
			}
			subnet = subnet.Masked()
			subnetStart, subnetEnd := prefixRange(subnet)
			for index, pool := range pools {
				poolStart, poolEnd := prefixRange(pool.prefix)
				first, last := max(subnetStart, poolStart), min(subnetEnd, poolEnd)
				if first > last {
					continue
				}
				blockSize := uint64(1) << (32 - pool.size)
				occupied[index] = append(occupied[index], slotInterval{
					start: (first - poolStart) / blockSize,
					end:   (last - poolStart) / blockSize,
				})
			}
		}
	}

	used := uint64(0)
	for _, intervals := range occupied {
		sort.Slice(intervals, func(i, j int) bool { return intervals[i].start < intervals[j].start })
		var end uint64
		for index, interval := range intervals {
			if index == 0 || interval.start > end {
				used += interval.end - interval.start + 1
			} else if interval.end > end {
				used += interval.end - end
			}
			end = max(end, interval.end)
		}
	}
	reserve := uint64(s.config.DockerNetworkReserveSubnets)
	if used >= configured || configured-used <= reserve {
		return 0, nil
	}
	available := (configured - used - reserve) / uint64(s.config.DockerNetworksPerRunner)
	return min(int(available), s.config.MaxRunners), nil
}

func prefixRange(prefix netip.Prefix) (uint64, uint64) {
	address := prefix.Masked().Addr().As4()
	start := uint64(address[0])<<24 | uint64(address[1])<<16 | uint64(address[2])<<8 | uint64(address[3])
	return start, start + (uint64(1) << (32 - prefix.Bits())) - 1
}
