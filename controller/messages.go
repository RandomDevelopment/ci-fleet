package main

import (
	"context"
	"fmt"
	"sync/atomic"
	"time"

	"github.com/actions/scaleset"
	"github.com/actions/scaleset/listener"
)

type diskAwareSession struct {
	listener.Client
	disk   *diskAdmission
	pollAt *atomic.Int64
}

func (s diskAwareSession) GetMessage(ctx context.Context, lastMessageID, maxCapacity int) (*scaleset.RunnerScaleSetMessage, error) {
	if !s.disk.check() {
		maxCapacity = 0
	}
	message, err := s.Client.GetMessage(ctx, lastMessageID, maxCapacity)
	if err == nil && ctx.Err() == nil && s.pollAt != nil {
		s.pollAt.Store(time.Now().Unix())
	}
	return message, err
}

func (s *Scaler) Scale(ctx context.Context, message *scaleset.RunnerScaleSetMessage) error {
	if err := ctx.Err(); err != nil {
		return err
	}
	if message != nil {
		if message.Statistics == nil {
			return fmt.Errorf("scale-set message has no statistics")
		}
		s.assignedJobs = message.Statistics.TotalAssignedJobs
		if message.MessageID != listener.InitialMessageID {
			// Finish an admitted batch through cleanup, as with the previous listener.
			ctx = context.WithoutCancel(ctx)
		}
		if len(message.JobAvailableMessages) > 0 && (s.disk == nil || s.disk.check()) {
			if s.messageClient == nil {
				return fmt.Errorf("job acquisition requires a message session")
			}
			ids := make([]int64, 0, len(message.JobAvailableMessages))
			for _, job := range message.JobAvailableMessages {
				ids = append(ids, job.RunnerRequestID)
			}
			acquired, err := s.messageClient.AcquireJobs(ctx, ids)
			if err != nil {
				return fmt.Errorf("acquire jobs: %w", err)
			}
			s.logger.Info("jobs acquired", "requested", len(ids), "acquired", len(acquired))
		}
		for _, job := range message.JobStartedMessages {
			if err := s.HandleJobStarted(ctx, job); err != nil {
				return err
			}
		}
		for _, job := range message.JobCompletedMessages {
			if err := s.HandleJobCompleted(ctx, job); err != nil {
				return err
			}
		}
	}
	_, err := s.HandleDesiredRunnerCount(ctx, s.assignedJobs)
	return err
}
