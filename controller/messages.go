package main

import (
	"context"
	"fmt"

	"github.com/actions/scaleset"
	"github.com/actions/scaleset/listener"
)

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
		if len(message.JobAvailableMessages) > 0 {
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
