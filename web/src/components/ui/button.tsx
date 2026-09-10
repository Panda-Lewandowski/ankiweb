import { Slot } from '@radix-ui/react-slot';
import { cva, type VariantProps } from 'class-variance-authority';
import type { ButtonHTMLAttributes } from 'react';
import { cn } from '../../lib/utils';

const buttonVariants = cva('button', {
  variants: {
    variant: {
      default: 'button--default',
      quiet: 'button--quiet',
      danger: 'button--danger',
    },
    size: {
      default: 'button--default-size',
      icon: 'button--icon',
    },
  },
  defaultVariants: { variant: 'default', size: 'default' },
});

type ButtonProps = ButtonHTMLAttributes<HTMLButtonElement> &
  VariantProps<typeof buttonVariants> & { asChild?: boolean };

export function Button({ className, variant, size, asChild, ...props }: ButtonProps) {
  const Component = asChild ? Slot : 'button';
  return <Component className={cn(buttonVariants({ variant, size }), className)} {...props} />;
}
