// Copyright 2016-2024, Pulumi Corporation.
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.

package state

import (
	"context"
	"errors"
	"fmt"

	"github.com/pulumi/pulumi/pkg/v3/backend/display"
	"github.com/pulumi/pulumi/pkg/v3/cmd/pulumi/backend"
	"github.com/pulumi/pulumi/pkg/v3/resource/deploy"
	"github.com/pulumi/pulumi/pkg/v3/resource/edit"
	pkgWorkspace "github.com/pulumi/pulumi/pkg/v3/workspace"
	"github.com/pulumi/pulumi/sdk/v3/go/common/env"
	"github.com/pulumi/pulumi/sdk/v3/go/common/resource"
	"github.com/pulumi/pulumi/sdk/v3/go/common/util/cmdutil"
	"github.com/pulumi/pulumi/sdk/v3/go/common/util/contract"

	"github.com/spf13/cobra"
)

func newStateUnprotectCommand() *cobra.Command {
	var unprotectAll bool
	var stack string
	var yes bool

	cmd := &cobra.Command{
		Use:   "unprotect [resource URN]",
		Short: "Unprotect resources in a stack's state",
		Long: `Unprotect resources in a stack's state

This command clears the 'protect' bit on one or more resources, allowing those resources to be deleted.

To see the list of URNs in a stack, use ` + "`pulumi stack --show-urns`" + `.`,
		Args: cmdutil.MaximumNArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			ctx := cmd.Context()
			ws := pkgWorkspace.Instance
			yes = yes || env.SkipConfirmations.Value()
			// Show the confirmation prompt if the user didn't pass the --yes parameter to skip it.
			showPrompt := !yes

			if unprotectAll {
				return unprotectAllResources(ctx, ws, stack, showPrompt)
			}

			var urn resource.URN

			if len(args) != 1 {
				if !cmdutil.Interactive() {
					return missingNonInteractiveArg("resource URN")
				}
				var err error
				urn, err = getURNFromState(ctx, ws, backend.DefaultLoginManager, stack, nil, "Select a resource to unprotect:")
				if err != nil {
					return fmt.Errorf("failed to select resource: %w", err)
				}
			} else {
				urn = resource.URN(args[0])
			}
			return unprotectResource(ctx, ws, stack, urn, showPrompt)
		},
	}

	cmd.PersistentFlags().StringVarP(
		&stack, "stack", "s", "",
		"The name of the stack to operate on. Defaults to the current stack")
	cmd.Flags().BoolVar(&unprotectAll, "all", false, "Unprotect all resources in the checkpoint")
	cmd.Flags().BoolVarP(&yes, "yes", "y", false, "Skip confirmation prompts")

	return cmd
}

func unprotectAllResources(ctx context.Context, ws pkgWorkspace.Context, stackName string, showPrompt bool) error {
	err := runTotalStateEdit(
		ctx, ws, backend.DefaultLoginManager, stackName, showPrompt, func(_ display.Options, snap *deploy.Snapshot) error {
			// Protects against Panic when a user tries to unprotect non-existing resources
			if snap == nil {
				return errors.New("no resources found to unprotect")
			}

			for _, res := range snap.Resources {
				err := edit.UnprotectResource(snap, res)
				contract.AssertNoErrorf(err, "Unable to unprotect resource %q", res.URN)
			}

			return nil
		})
	if err != nil {
		return err
	}
	fmt.Println("All resources unprotected")
	return nil
}

func unprotectResource(
	ctx context.Context, ws pkgWorkspace.Context, stackName string, urn resource.URN, showPrompt bool,
) error {
	err := runStateEdit(ctx, ws, backend.DefaultLoginManager, stackName, showPrompt, urn, edit.UnprotectResource)
	if err != nil {
		return err
	}
	fmt.Println("Resource unprotected")
	return nil
}
